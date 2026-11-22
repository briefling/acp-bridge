"""The bridge: one outbound WebSocket to the briefling hub (same wire as the Hermes plugin, protocol v2), one ACP agent
process, one ACP session per briefling conversation (thread or Lounge topic).

Hub -> bridge: `deliver` (a human's message), `turn.stop` (Stop button), `subscribed`, `error`, and the answers to
the tool frames (`ack`, `action.result`, `memory.result`, `introspect.result`, `memory.proposed`, `error` with `ref`).
Bridge -> hub: `subscribe`, `turn.started` / `turn.ended`, `message.final` (one message per turn, streamed through
successive edits: same msg_id, growing rev), `status` (tool in progress, ephemeral), `ping`, and the frames of
briefling's tools (`thread.outcome`, `thread.propose`, `issue.propose`, `about.propose`, `action.acquire|release`,
`ci.watch`, `memory.search|read|propose`), the same as the Hermes plugin's.

Tools: the agent gets them from a local MCP server (`briefling-acp mcp`, declared in `session/new`), which relays each
call to this process over a Unix socket in a directory only this user can open (Windows, which has no Unix socket in
asyncio: TCP on 127.0.0.1, any free port, each call carrying a random key read from an endpoint file at the socket's
place, in the same private directory; `mcp.py`). Each conversation's server carries
its own random token, so a call is tied to its conversation, and it only counts while that conversation has a turn
running (the hub checks the turn again). With `--mcp-port` (an agent in a microVM, which can start no host command),
the bridge also serves them over HTTP on 127.0.0.1 and declares that `http` server instead, same token in a header
(`mcp_http.py`).

Gates (the hub already applied its own: membership, roles): a message or a Stop counts only if its author is in
BRIEFLING_ALLOWED_USERS (the trusted humans the hub listed at join, Cognito subs). Empty list = nobody (fail closed).
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import time
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional, Set

import aiohttp
from aiohttp import web

from . import attachments as att
from . import tools as tl
from .acp import AcpAgent, AcpError
from .join import PROTOCOL, PROTOCOL_VERSION, proxy_for
from . import __version__, mcp_http, selfupdate
from .mcp import SOCKET_ENV, TOKEN_ENV
from .tooldefs import HUB_UNREACHABLE, NO_TEXT, with_context

UPDATE_IDLE_POLL_S = 5  # how often a bridge holding a new release looks for a moment with no turn

logger = logging.getLogger(__name__)

SCOPE = "briefling/instance"
MAX_TEXT = 32_000        # protocol/frames.ts MAX_TEXT
MAX_STATUS = 200         # protocol/frames.ts MAX_STATUS
MAX_ACTIVITY = 10        # protocol/frames.ts MAX_ACTIVITY
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RECONNECT_BACKOFF = [1, 2, 5, 10, 30, 60]
PING_EVERY_S = 300       # API Gateway closes idle connections after 10 min
RECYCLE_AFTER_S = 110 * 60  # ... and every connection after 2 h
TOKEN_MARGIN_S = 120
EDIT_EVERY_S = 1.0       # streamed reply: at most one edit per second (each edit is a hub write)
# The hub runs the frames of one connection in concurrent Lambdas and refuses `status` outside a turn (no_turn): a status
# sent right after turn.started can be handled first (seen live). Status frames wait this long after the turn start.
STATUS_AFTER_S = 1.0
DEDUP_KEEP = 2000
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"

NO_REPLY = "(the agent ended its turn without a reply: {reason})"
AGENT_DOWN = "(the agent could not run this turn: {why}. The bridge log on the agent host has the details{hint}.)"
KIRO_HINT = "; for Kiro, check `kiro-cli whoami`"


def agent_down(command: List[str], why: str) -> str:
    """The turn's error line; the Kiro login hint only when the agent is Kiro (Qwen, opencode... have their own)."""
    name = re.split(r"[\\/]", command[0])[-1].lower() if command else ""
    return AGENT_DOWN.format(why=why, hint=KIRO_HINT if name.startswith("kiro") else "")


REFUSED = "Refused (not pre-authorized in the agent's own configuration): {title}"
TOOLS_HINT = ("briefling's tools need a one-time allow rule in the agent's config (Kiro: `@briefling` in the agent's "
              "allowedTools, or a `mcp` allow rule for `briefling/*` in V3 permissions); see the bridge README.")
MCP_SERVER = "briefling"   # the MCP server's name in the agent (Kiro: tools @briefling/<tool>)
REQUEST_TIMEOUT_S = 20.0   # the hub's answer to a tool frame (as the plugin)
SOCKET_LINE_MAX = 1 << 20


def new_id() -> str:
    """Time-sortable id (base36 ms + 12 hex), same shape as the hub's (hermes-plugin adapter.new_id)."""
    n, out = int(time.time() * 1000), ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out + secrets.token_hex(6)


def frame_files(frame: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The attachments of a deliver: `files` (the message's and those of the unseen messages, with a read URL and
    `by`), else the message's own (an older hub: listed, read with attachment_get)."""
    msg = frame.get("message") or {}
    files = frame.get("files") if isinstance(frame.get("files"), list) else msg.get("files")
    return [f for f in (files or []) if isinstance(f, dict) and isinstance(f.get("id"), str)]


def poster_names(frame: Dict[str, Any]) -> Dict[str, str]:
    """Who posted a file, as the listing says it: the message's author by name, this agent as « you »."""
    msg = frame.get("message") or {}
    out = {str(frame.get("agent") or ""): "you"}
    author = str((msg.get("author") or {}).get("id") or "")
    if author:
        out[author] = str(frame.get("author_name") or "the author of the message")
    return out


def prompt_text(frame: Dict[str, Any], sent: Optional[Set[str]] = None) -> str:
    """What the agent receives: one line (where, who), the list of attachments (`sent`: ids whose content comes with
    the prompt), then the same context as a Hermes agent (space description, resources, repos, Lounge rules, unseen
    messages quoted as data: the plugin's `with_context`), then the text."""
    ctx = frame.get("context") if isinstance(frame.get("context"), dict) else {}
    msg = frame.get("message") or {}
    kind = "Lounge topic" if ctx.get("kind") == "topic" else "thread"
    title = str(ctx.get("title") or "")[:200]
    who = str(frame.get("author_name") or "a human")[:100]
    head = f"[briefling {kind} \"{title}\", message from {who}]\n"
    head += att.listing(frame_files(frame), sent or set(), {k: v for k, v in poster_names(frame).items() if k})
    return head + with_context(ctx, str(msg.get("text") or ""))


def is_briefling_tool(title: str) -> bool:
    """A permission request about one of briefling's tools (only used for a hint: the request is refused anyway)."""
    return any(re.search(rf"(?<![A-Za-z0-9_]){n}(?![A-Za-z0-9_])", title) for n in tl.NAMES)


class HubRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def socket_dir() -> str:
    """A fresh private directory for the socket (tests, or no credentials file)."""
    return tempfile.mkdtemp(prefix="briefling-acp-")


def one_line(value: Any) -> Optional[str]:
    """The first non-blank line of a label, cut to MAX_STATUS (a status shows one line)."""
    line = next((x.strip() for x in str(value or "").splitlines() if x.strip()), "")
    return line[:MAX_STATUS] or None


def tool_label(u: Dict[str, Any], kind: str) -> Optional[str]:
    """What members see of an action. A command (`kind: execute`) never shows its title, the raw command line (an
    `AWS_SECRET_ACCESS_KEY=…` would show, and the bridge has no Hermes redactor to mask it): only the description the
    agent wrote in its input (claude-agent-acp: Bash `description`), else nothing. Read from `rawInput`, never from
    `content`, which carries the command's output once it ran."""
    if kind == "execute":
        raw = u.get("rawInput") if isinstance(u.get("rawInput"), dict) else {}
        return one_line(raw.get("description") or raw.get("command_description"))
    return one_line(u.get("title"))


class Turn:
    """One running prompt turn of a conversation: accumulates the streamed reply and relays it."""

    def __init__(self, bridge: "Bridge", thread: str):
        self.bridge, self.thread = bridge, thread
        self.msg_id = new_id()
        self.text = ""
        self.refused: List[str] = []
        self.tools: deque = deque(maxlen=MAX_ACTIVITY)
        self.tool_ids: deque = deque(maxlen=MAX_ACTIVITY)  # toolCallId of each entry of `tools`
        self.kinds: Dict[str, str] = {}  # toolCallId -> kind (an update may not repeat it)
        self.steps = 0  # every tool call of the turn (`tools` keeps only the last MAX_ACTIVITY)
        self.trailing: Optional[asyncio.Task] = None  # deferred edit of text held back by the throttle
        self.tools_at: deque = deque(maxlen=MAX_ACTIVITY)  # start (epoch ms) of each entry of `tools`: status.recentAt
        self.acted = False
        self.sent = ""  # last text relayed
        self.last_edit = 0.0
        self.since = int(time.time() * 1000)
        self.started = time.monotonic()
        self.pending_status: Optional[str] = None
        self.late: Optional[asyncio.Task] = None
        self.after_tool = False  # a tool ran since the last text: the next chunk opens a new paragraph

    def reply(self) -> str:
        notes = "\n".join(f"_{t if t == TOOLS_HINT else REFUSED.format(title=t)}_" for t in self.refused)
        return (self.text.strip() + ("\n\n" + notes if notes else "")).strip()

    async def flush(self, force: bool = False) -> None:
        body = self.reply()[:MAX_TEXT]
        if not body or body == self.sent:
            return
        wait = EDIT_EVERY_S - (time.monotonic() - self.last_edit)
        if not force and wait > 0:  # held back: sent when the window ends, never left frozen mid-word
            if self.trailing is None:
                self.trailing = asyncio.create_task(self._trailing(wait))
            return
        self.sent, self.last_edit = body, time.monotonic()
        await self.bridge.send({"type": "message.final", "thread": self.thread, "msg_id": self.msg_id, "text": body,
                                "rev": self.bridge.next_rev()})

    async def _trailing(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self.trailing = None
        if self.bridge.turns.get(self.thread) is self:
            await self.flush(force=True)

    async def status(self, text: Optional[str]) -> None:
        early = STATUS_AFTER_S - (time.monotonic() - self.started)
        if early > 0:  # too close to turn.started: send the latest one when the delay is over
            self.pending_status = text
            if self.late is None:
                self.late = asyncio.create_task(self._late_status(early))
            return
        await self._send_status(text)

    async def _late_status(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.bridge.turns.get(self.thread) is self:
            await self._send_status(self.pending_status)

    async def _send_status(self, text: Optional[str]) -> None:
        await self.bridge.send({"type": "status", "thread": self.thread, "text": text, "rev": self.bridge.next_rev(),
                                "since": self.since, **({"steps": self.steps} if self.steps else {}),
                                **({"recent": list(self.tools), "recentAt": list(self.tools_at)} if self.tools else {})})

    async def on_update(self, u: Dict[str, Any]) -> None:
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = u.get("content") or {}
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                # Narration on both sides of a tool call would otherwise fuse (« …existant.Le travail avance »).
                if self.after_tool and self.text and not self.text.endswith("\n") and not c["text"].startswith("\n"):
                    self.text += "\n\n"
                self.after_tool = False
                self.text += c["text"]
                await self.flush()
        elif kind == "tool_call":
            await self.flush(force=True)  # the text so far goes out before the action, not after it
            self.acted = True
            self.after_tool = True
            self.steps += 1
            tk, cid = str(u.get("kind") or ""), str(u.get("toolCallId") or "")
            self.kinds[cid] = tk
            title = tool_label(u, tk) or ("Terminal" if tk == "execute" else tk or "tool")
            self.tools.append(title)
            self.tool_ids.append(cid)
            self.tools_at.append(int(time.time() * 1000))
            await self.status(title)
        elif kind == "tool_call_update" and ("title" in u or "rawInput" in u):
            # claude-agent-acp sends `tool_call` before the input streams in (« Terminal », « Preparing file… »),
            # then the real title in an update of the same toolCallId: rename that entry, never add one. A command's
            # description comes later still, in an update with `rawInput` only (an unchanged title is left out).
            cid = str(u.get("toolCallId") or "")
            if not cid or cid not in self.tool_ids:
                return
            tk = str(u.get("kind") or self.kinds.get(cid) or "")
            self.kinds[cid] = tk
            title = tool_label(u, tk)
            i = list(self.tool_ids).index(cid)
            if title and self.tools[i] != title:
                self.tools[i] = title
                await self.status(self.tools[-1])

    async def on_permission(self, p: Dict[str, Any]) -> None:
        tc = p.get("toolCall") if isinstance(p.get("toolCall"), dict) else {}
        title = str(tc.get("title") or tc.get("kind") or "a tool call")[:MAX_STATUS]
        logger.warning("permission refused in thread %s: %s", self.thread, title)
        self.refused.append(title)
        if is_briefling_tool(title) and TOOLS_HINT not in self.refused:
            self.refused.append(TOOLS_HINT)
        await self.flush(force=True)


class Bridge:
    def __init__(self, env: Dict[str, str], command: List[str], cwd: str, state_path: Optional[str] = None,
                 socket_path: Optional[str] = None, mcp_port: Optional[int] = None, mcp_host: str = "127.0.0.1",
                 update_path: Optional[str] = None, mcp_tcp: Optional[bool] = None):
        self.ws_url = env.get("BRIEFLING_WS_URL", "")
        self.token_url = env.get("BRIEFLING_TOKEN_URL", "")
        self.client_id = env.get("BRIEFLING_CLIENT_ID", "")
        self.client_secret = env.get("BRIEFLING_CLIENT_SECRET", "")
        self.allowed: Set[str] = {u.strip() for u in env.get("BRIEFLING_ALLOWED_USERS", "").split(",") if u.strip()}
        if not all((self.ws_url, self.token_url, self.client_id, self.client_secret)):
            raise ValueError("incomplete config: run `join` first (BRIEFLING_WS_URL/TOKEN_URL/CLIENT_ID/CLIENT_SECRET)")
        if not self.allowed:
            logger.warning("BRIEFLING_ALLOWED_USERS is empty: every message will be dropped")
        self.agent = AcpAgent(command, cwd)
        self.command = command
        self.state_path = state_path
        self.sessions: Dict[str, str] = self._load_state()  # thread -> ACP sessionId
        self.loaded: Set[str] = set()  # sessions known to the current agent process
        self.agent_pid: Optional[int] = None
        self.turns: Dict[str, Turn] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        self.stop_by: Dict[str, str] = {}
        self.tasks: Set[asyncio.Task] = set()
        self.seen: "OrderedDict[str, None]" = OrderedDict()
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.send_lock = asyncio.Lock()
        self.token: Optional[str] = None
        self.token_exp = 0.0
        self.rev = 0
        self.last_turn_at: Dict[str, int] = {}
        self.running = False
        # Self-update (selfupdate.py): on unless BRIEFLING_ACP_AUTO_UPDATE=0; update_path = its record (None = off).
        self.update_path = update_path
        self.auto_update = update_path is not None and selfupdate.enabled(env.get(selfupdate.OFF_ENV))
        self.install: Optional[selfupdate.Install] = None
        self.updated_from = os.environ.pop(selfupdate.FROM_ENV, "") or None
        self.update_task: Optional[asyncio.Task] = None
        self.restart = False   # a release is installed: run() returns and the caller re-executes the bridge
        self.in_flight = 0     # turns delivered and not ended yet (waiting on their conversation's lock included)
        self.connected = asyncio.Event()
        self.waiting: Dict[str, asyncio.Future] = {}  # client_id -> hub answer to a tool frame
        self.socket_path = socket_path
        self.socket_server: Optional[asyncio.AbstractServer] = None
        # Windows: loopback TCP + key instead of the Unix socket (None = by platform; True on Unix for the tests).
        self.mcp_tcp = sys.platform == "win32" if mcp_tcp is None else mcp_tcp
        self.socket_key = ""
        self.tokens: Dict[str, str] = {}  # MCP token -> thread (this process only: a restart issues new ones)
        self.thread_token: Dict[str, str] = {}
        self.mcp_port = mcp_port   # HTTP MCP server on 127.0.0.1 (None = off; 0 = any free port, tests)
        self.mcp_host = mcp_host   # the host name the agent uses to reach it (sbx: host.docker.internal)
        self.mcp_runner: Optional[web.AppRunner] = None
        self._warned_no_http = False

    # -- state ----------------------------------------------------------------------

    def _load_state(self) -> Dict[str, str]:
        if not self.state_path or not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            return {k: v for k, v in data.get("sessions", {}).items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"sessions": self.sessions}, f)
        os.replace(tmp, self.state_path)

    def next_rev(self) -> int:
        self.rev = max(self.rev + 1, int(time.time() * 1000))
        return self.rev

    def turn_at(self, thread: str) -> int:
        at = max(int(time.time() * 1000), self.last_turn_at.get(thread, 0) + 1)
        self.last_turn_at[thread] = at
        return at

    # -- hub connection ---------------------------------------------------------------

    async def access_token(self) -> str:
        if self.token and time.time() < self.token_exp - TOKEN_MARGIN_S:
            return self.token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        async with self.session.post(self.token_url, data={"grant_type": "client_credentials", "scope": SCOPE},
                                     headers={"Authorization": f"Basic {basic}"}, proxy=proxy_for(self.token_url),
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise RuntimeError(f"token endpoint HTTP {r.status}")  # body may echo the request: not logged
            body = await r.json()
        self.token = body["access_token"]
        self.token_exp = time.time() + int(body.get("expires_in", 3600))
        return self.token

    async def send(self, frame: Dict[str, Any]) -> bool:
        ws = self.ws
        if ws is None or ws.closed:
            return False
        async with self.send_lock:
            try:
                await ws.send_str(json.dumps({"v": PROTOCOL_VERSION, **frame}))
                return True
            except Exception as e:
                logger.warning("send to hub failed: %s", type(e).__name__)
                return False

    async def run(self) -> None:
        self.running = True
        try:  # fail fast on the operator's terminal (not logged in, wrong command) rather than at the first message
            await self.agent.ensure_started()
        except (AcpError, OSError) as e:
            await asyncio.sleep(0.2)  # let the stderr reader catch the agent's own explanation
            logger.error("the agent command failed to start: %s. Its stderr: %s", e, self.agent.stderr_tail.strip()[-800:] or "-")
            await self.agent.stop()
            raise SystemExit(1)
        await self._start_socket()
        try:
            await self._start_http()
        except OSError as e:
            logger.error("cannot serve briefling's MCP tools on 127.0.0.1:%s: %s", self.mcp_port, e)
            await self.close()
            raise SystemExit(1)
        self.session = aiohttp.ClientSession()
        backoff = 0
        try:
            while self.running:
                opened = time.monotonic()
                try:
                    token = await self.access_token()
                    self.ws = await self.session.ws_connect(self.ws_url, protocols=(PROTOCOL, token), heartbeat=None,
                                                            proxy=proxy_for(self.ws_url),
                                                            timeout=aiohttp.ClientWSTimeout(ws_close=10))
                    if self.ws.protocol != PROTOCOL:
                        raise RuntimeError(f"hub did not accept the {PROTOCOL} subprotocol")
                    await self.send({"type": "subscribe", "bridge": await self.bridge_report()})
                    logger.info("connected to the hub")
                    self.connected.set()
                    await self._read_loop(opened)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # network, 401/403 at handshake, token endpoint
                    logger.warning("hub connection error: %s", type(e).__name__)
                finally:
                    self.connected.clear()
                    if self.ws is not None and not self.ws.closed:
                        await self.ws.close()
                if not self.running:
                    break
                if time.monotonic() - opened > 60:
                    backoff = 0
                await asyncio.sleep(RECONNECT_BACKOFF[min(backoff, len(RECONNECT_BACKOFF) - 1)])
                backoff += 1
        finally:
            await self.close()

    async def close(self) -> None:
        self.running = False
        for t in list(self.tasks):
            t.cancel()
        await self.agent.stop()
        if self.socket_server is not None:
            self.socket_server.close()
            self.socket_server = None
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        if self.mcp_runner is not None:
            await self.mcp_runner.cleanup()
            self.mcp_runner = None
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self.session:
            await self.session.close()
            self.session = None

    async def _read_loop(self, opened: float) -> None:
        next_ping = time.monotonic() + PING_EVERY_S
        while self.running:
            if time.monotonic() - opened > RECYCLE_AFTER_S:
                logger.info("recycling the hub connection before the 2 h limit")
                return
            try:
                msg = await self.ws.receive(timeout=max(1.0, next_ping - time.monotonic()))
            except asyncio.TimeoutError:
                await self.send({"type": "ping"})
                next_ping = time.monotonic() + PING_EVERY_S
                continue
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    frame = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    self.on_frame(frame)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                              aiohttp.WSMsgType.CLOSING):
                return

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def on_frame(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "deliver":
            self.on_deliver(frame)
        elif kind == "turn.stop":
            self._spawn(self.on_stop(frame))
        elif kind == "subscribed":
            if frame.get("denied"):
                logger.warning("hub refused spaces: %s", ",".join(map(str, frame["denied"])))
            self.on_release(frame.get("release"))
        elif kind == "release":  # pushed by the hub right after a deploy
            self.on_release(frame.get("release"))
        elif kind in ("ack", "resource.result", "action.result", "memory.result", "introspect.result", "memory.proposed", "files.urls"):
            fut = self.waiting.pop(str(frame.get("client_id")), None)
            if fut and not fut.done():
                fut.set_result(frame)
        elif kind == "error":
            fut = self.waiting.pop(str(frame.get("ref")), None)
            if fut and not fut.done():
                fut.set_exception(HubRefused(str(frame.get("code") or "error"), str(frame.get("detail") or "")[:400]))
            logger.warning("hub error: %s %s", frame.get("code"), str(frame.get("detail") or "")[:200])

    # -- turns ------------------------------------------------------------------------

    def on_deliver(self, frame: Dict[str, Any]) -> None:
        msg = frame.get("message") or {}
        thread, msg_id = frame.get("thread"), msg.get("id")
        if not (isinstance(thread, str) and ID_RE.match(thread) and isinstance(msg_id, str) and msg.get("text")):
            return
        key = f"{msg_id}@{msg.get('edited')}" if msg.get("edited") else msg_id
        if key in self.seen:
            return
        self.seen[key] = None
        while len(self.seen) > DEDUP_KEEP:
            self.seen.popitem(last=False)
        author = str((msg.get("author") or {}).get("id") or "")
        if author not in self.allowed:
            logger.warning("message dropped in %s: author not in BRIEFLING_ALLOWED_USERS", thread)
            return
        self.in_flight += 1
        self._spawn(self.run_turn(thread, frame))

    async def _session_for(self, thread: str) -> str:
        await self.agent.ensure_started()
        if self.agent.proc.pid != self.agent_pid:  # a new process: it knows none of the sessions yet
            self.agent_pid, self.loaded = self.agent.proc.pid, set()
        sid = self.sessions.get(thread)
        if sid and (sid in self.loaded or await self.agent.load_session(sid, self.mcp_servers(thread))):
            self.loaded.add(sid)
            return sid
        sid = await self.agent.new_session(self.mcp_servers(thread))
        self.sessions[thread] = sid
        self.loaded.add(sid)
        self._save_state()
        return sid

    async def run_turn(self, thread: str, frame: Dict[str, Any]) -> None:
        lock = self.locks.setdefault(thread, asyncio.Lock())
        async with lock:  # one turn at a time per conversation; a message sent meanwhile waits its turn
            turn = Turn(self, thread)
            self.turns[thread] = turn
            self.stop_by.pop(thread, None)
            await self.send({"type": "turn.started", "thread": thread, "at": self.turn_at(thread)})
            reason = "end_turn"
            try:
                sid = await self._session_for(thread)
                files = frame_files(frame)
                blocks, sent = await att.prompt_blocks(self.session, files, self.agent.capabilities.get("promptCapabilities") or {},
                                                       self.ws_url, proxy_for) if files else ([], set())
                reason = await self.agent.prompt(sid, prompt_text(frame, sent), turn.on_update, turn.on_permission, blocks)
            except (AcpError, OSError) as e:
                logger.warning("turn failed in %s: %s", thread, e)
                if self.agent.stderr_tail:
                    logger.warning("agent stderr (last lines): %s", self.agent.stderr_tail[-800:])
                reason = None
                turn.text += ("\n\n" if turn.text else "") + agent_down(self.command, type(e).__name__ if isinstance(e, OSError) else str(e)[:200])
            except Exception:
                logger.exception("turn crashed in %s", thread)
                reason = None
                turn.text += "\n\n(the bridge failed during this turn; see its log)"
            finally:
                stopped = self.stop_by.pop(thread, None) is not None
                if reason and reason not in ("end_turn", "cancelled") and not turn.text.strip():
                    turn.text = NO_REPLY.format(reason=reason)
                await turn.flush(force=True)
                self.turns.pop(thread, None)
                if turn.late:
                    turn.late.cancel()
                if turn.trailing:
                    turn.trailing.cancel()
                self.in_flight -= 1
                await self.send({"type": "turn.ended", "thread": thread, "at": self.turn_at(thread),
                                 **({"stopped": True, "acted": turn.acted} if stopped else {})})

    async def on_stop(self, frame: Dict[str, Any]) -> None:
        thread, by = frame.get("thread"), str(frame.get("by") or "")
        if not (isinstance(thread, str) and ID_RE.match(thread)):
            return
        if by not in self.allowed:
            logger.warning("stop ignored in %s: author not in BRIEFLING_ALLOWED_USERS", thread)
            return
        turn, sid = self.turns.get(thread), self.sessions.get(thread)
        if turn is None or sid is None:
            return  # the turn already ended
        self.stop_by[thread] = by
        await self.agent.cancel(sid)

    # -- briefling's tools (MCP server <-> Unix socket <-> hub) -------------------------

    def mcp_servers(self, thread: str) -> List[Dict[str, Any]]:
        """The MCP server declared in session/new|load for this conversation: this package's `mcp` subcommand, with
        the socket path and this conversation's token in its environment (ACP stdio server, mandatory for agents);
        or, with `--mcp-port` and an agent that takes HTTP servers, the bridge's own HTTP server, token in a header."""
        if self.socket_server is None:
            return []
        token = self.thread_token.get(thread)
        if token is None:
            token = secrets.token_urlsafe(32)
            self.thread_token[thread], self.tokens[token] = token, thread
        if self.mcp_runner is not None:
            if (self.agent.capabilities.get("mcpCapabilities") or {}).get("http"):
                return [{"type": "http", "name": MCP_SERVER, "url": f"http://{self.mcp_host}:{self.mcp_port}{mcp_http.PATH}",
                         "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}]
            if not self._warned_no_http:
                self._warned_no_http = True
                logger.warning("--mcp-port: the agent does not advertise MCP over HTTP (mcpCapabilities.http): "
                               "declaring the local stdio server instead")
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return [{"name": MCP_SERVER, "command": sys.executable, "args": ["-m", "briefling_acp", "mcp"],
                 "env": [{"name": SOCKET_ENV, "value": self.socket_path}, {"name": TOKEN_ENV, "value": token},
                         {"name": "PYTHONPATH", "value": pkg_parent}]}]

    async def _start_socket(self) -> None:
        if self.socket_path is None:
            self.socket_path = os.path.join(socket_dir(), "mcp.sock")
        d = os.path.dirname(self.socket_path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)  # macOS ignores a socket's own mode: the directory is the gate
        try:
            os.unlink(self.socket_path)  # left by a previous run
        except FileNotFoundError:
            pass
        if self.mcp_tcp:
            await self._start_tcp()
            return
        old = os.umask(0o177)
        try:
            self.socket_server = await asyncio.start_unix_server(self._on_socket, path=self.socket_path,
                                                                 limit=SOCKET_LINE_MAX)
        finally:
            os.umask(old)
        os.chmod(self.socket_path, 0o600)

    async def _start_tcp(self) -> None:
        """The Unix socket's stand-in: 127.0.0.1 on a free port, and at the socket's path a file {port, key} that only
        this user can read (private directory; Windows: the user profile's own ACL). Any local process can open the port,
        so every call must carry the key, the token-less fallback included."""
        self.socket_key = secrets.token_urlsafe(32)
        self.socket_server = await asyncio.start_server(self._on_socket, "127.0.0.1", 0, limit=SOCKET_LINE_MAX)
        port = self.socket_server.sockets[0].getsockname()[1]
        tmp = self.socket_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"port": port, "key": self.socket_key}, f)
        os.replace(tmp, self.socket_path)

    # -- self-update (selfupdate.py) ----------------------------------------------------

    async def bridge_report(self) -> Dict[str, Any]:
        """`subscribe.bridge`: this version, why it will not update by itself, and the version it was updated from
        (first connection after a self-update only: the hub posts one notice)."""
        out: Dict[str, Any] = {"version": __version__}
        if self.update_path is not None:
            if self.install is None:
                self.install = await asyncio.get_running_loop().run_in_executor(None, selfupdate.Install.here)
            why = selfupdate.blocked(self.install, self.auto_update, __version__, selfupdate.read_state(self.update_path))
            if why:
                out["update"] = why
        if self.updated_from:
            out["updated_from"], self.updated_from = self.updated_from, None
        return out

    def on_release(self, release: Any) -> None:
        rel = selfupdate.valid_release(release)
        if not self.auto_update or rel is None or not selfupdate.is_newer(rel["version"], __version__):
            return
        if self.update_task is not None and not self.update_task.done():
            return  # one at a time
        self.update_task = asyncio.create_task(self._self_update(rel))

    async def _self_update(self, rel: Dict[str, str]) -> None:
        loop = asyncio.get_running_loop()
        if self.install is None:
            self.install = await loop.run_in_executor(None, selfupdate.Install.here)
        ok = await loop.run_in_executor(None, selfupdate.apply, self.install, rel, __version__, self.update_path)
        if not ok:
            return
        while self.running and (self.in_flight or self.turns):
            await asyncio.sleep(UPDATE_IDLE_POLL_S)
        if not self.running:
            return
        logger.info("restarting into bridge %s", rel["version"])
        self.restart, self.running = True, False
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()  # ends the read loop; run() then closes the rest and returns

    async def _start_http(self) -> None:
        """The HTTP MCP server (`--mcp-port`): loopback only, whatever the host name the agent is given."""
        if self.mcp_port is None:
            return
        runner = web.AppRunner(mcp_http.app(lambda tok: self.tokens.get(tok), self.tool_call), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.mcp_port)
        try:
            await site.start()
        except OSError:
            await runner.cleanup()
            raise
        self.mcp_runner = runner
        self.mcp_port = site._server.sockets[0].getsockname()[1]
        logger.info("briefling's MCP tools also served on http://127.0.0.1:%s%s (agent URL host: %s)",
                    self.mcp_port, mcp_http.PATH, self.mcp_host)

    async def _on_socket(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            try:
                req = json.loads(line)
            except ValueError:
                req = None
            if not isinstance(req, dict):
                res = {"ok": False, "error": "bad_request"}
            elif self.socket_key and not secrets.compare_digest(str(req.get("key") or ""), self.socket_key):
                res = {"ok": False, "error": "forbidden", "detail": "wrong bridge key"}
            else:
                res = await self.tool_call(req)
            writer.write((json.dumps(res, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()

    def _thread_of(self, token: str) -> Optional[str]:
        if token:
            return self.tokens.get(token)
        # Server declared in the agent's own config (no token): only unambiguous with exactly one turn running.
        running = list(self.turns)
        return running[0] if len(running) == 1 else None

    async def tool_call(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """One call from the MCP server: check token, turn and arguments, send the frame, shape the hub's answer."""
        tool, args, token = req.get("tool"), req.get("args"), str(req.get("token") or "")
        if tool not in tl.BUILD or not isinstance(args, dict):
            return {"ok": False, "error": "bad_request", "detail": "unknown tool"}
        thread = self._thread_of(token)
        if thread is None:
            if token:
                return {"ok": False, "error": "forbidden", "detail": "unknown token: restart this conversation's session"}
            return {"ok": False, "error": "no_turn", "detail": (
                "the bridge cannot tell which conversation this call belongs to: no turn, or several at once. "
                "This happens when briefling's MCP server comes from the agent's own config instead of the session.")}
        turn = self.turns.get(thread)
        if turn is None:
            return {"ok": False, "error": "no_turn", "detail": "Only during a turn triggered by a trusted human in this conversation."}
        frame, err = tl.BUILD[tool](args)
        if err:
            return {"ok": False, "error": "bad_args", "detail": err}
        early = STATUS_AFTER_S - (time.monotonic() - turn.started)
        if early > 0:  # like status: a frame right after turn.started can be handled first and refused no_turn (seen live)
            await asyncio.sleep(early)
        if tool == "turn_outcome":
            text = str(args.get("text") or "").strip()  # the answer written in the same step: posted with the turn's message
            if text:
                turn.text += ("\n\n" if turn.text.strip() else "") + text + "\n\n"  # later text: its own paragraph
            if frame["outcome"] == "waiting" and not turn.reply():
                return {"ok": False, "error": "no_text", "detail": NO_TEXT}
            if text or frame["outcome"] == "waiting":
                await turn.flush(force=True)  # the human sees the answer before the banner
        logger.info("tool %s in %s", tool, thread)
        try:
            answer = await self.request({**frame, "thread": thread})
        except HubRefused as e:
            return tl.refused(tool, e.code, e.detail)
        except (ConnectionError, asyncio.TimeoutError):
            return dict(HUB_UNREACHABLE)
        if tool == "attachment_get":
            return await self._attachment(frame["ids"][0], answer)
        return tl.result(tool, frame, answer)

    async def _attachment(self, fid: str, answer: Dict[str, Any]) -> Dict[str, Any]:
        """attachment_get, once the hub gave (or not) a fresh URL: the bridge downloads it (attachments.read_one)."""
        urls = answer.get("urls") if isinstance(answer.get("urls"), dict) else {}
        refs = [f for f in (answer.get("files") or []) if isinstance(f, dict) and f.get("id") == fid]
        url = urls.get(fid)
        if not refs or not att.url_ok(url, self.ws_url) or self.session is None:
            return {"ok": False, "error": "not_found", "detail": (
                "No such attachment that you may read: check the id in the list at the top of the message "
                "(only attachments of the spaces you serve, whose member is the human of this turn).")}
        return await att.read_one(self.session, refs[0], url, proxy_for(url))

    async def request(self, frame: Dict[str, Any], timeout: float = REQUEST_TIMEOUT_S) -> Dict[str, Any]:
        """Send a frame with a fresh client_id and wait for the hub's answer (or its `error` with that ref)."""
        cid = new_id()
        fut = asyncio.get_running_loop().create_future()
        self.waiting[cid] = fut
        try:
            if not await self.send({**frame, "client_id": cid}):
                raise ConnectionError("hub not connected")
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.waiting.pop(cid, None)
