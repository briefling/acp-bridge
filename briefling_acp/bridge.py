"""The bridge: one outbound WebSocket to the briefling hub (same wire as the Hermes plugin, protocol v2), one ACP agent
process, one ACP session per briefling conversation (thread or Lounge topic).

Hub -> bridge: `deliver` (a human's message), `turn.stop` (Stop button), `subscribed`, `error`.
Bridge -> hub: `subscribe`, `turn.started` / `turn.ended`, `message.final` (one message per turn, streamed through
successive edits: same msg_id, growing rev), `status` (tool in progress, ephemeral), `ping`.

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
import time
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional, Set

import aiohttp

from .acp import AcpAgent, AcpError
from .join import PROTOCOL, PROTOCOL_VERSION

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
AGENT_DOWN = ("(the agent could not run this turn: {why}. The bridge log on the agent host has the details; "
              "for Kiro, check `kiro-cli whoami`.)")
REFUSED = "Refused (not pre-authorized in the agent's own configuration): {title}"


def new_id() -> str:
    """Time-sortable id (base36 ms + 12 hex), same shape as the hub's (hermes-plugin adapter.new_id)."""
    n, out = int(time.time() * 1000), ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out + secrets.token_hex(6)


def prompt_text(frame: Dict[str, Any]) -> str:
    """What the agent receives: one line of context (where, who), then the human's text as is."""
    ctx = frame.get("context") if isinstance(frame.get("context"), dict) else {}
    msg = frame.get("message") or {}
    kind = "Lounge topic" if ctx.get("kind") == "topic" else "thread"
    title = str(ctx.get("title") or "")[:200]
    who = str(frame.get("author_name") or "a human")[:100]
    head = f"[briefling {kind} \"{title}\", message from {who}]"
    unseen = [u for u in (ctx.get("unseen") or []) if isinstance(u, dict) and isinstance(u.get("text"), str)]
    if unseen:  # messages of the conversation the agent has not seen: quoted data, not instructions
        quoted = "\n".join(f"> {str(u.get('who') or '?')[:100]}: " + u["text"][:4000].replace("\n", "\n> ") for u in unseen[-20:])
        head += f"\n[Earlier messages in this conversation, for context only:]\n{quoted}"
    files = msg.get("files") or frame.get("files") or []
    if files:
        head += f"\n[{len(files)} attachment(s) not forwarded: this bridge relays text only]"
    return f"{head}\n\n{msg.get('text') or ''}"


class Turn:
    """One running prompt turn of a conversation: accumulates the streamed reply and relays it."""

    def __init__(self, bridge: "Bridge", thread: str):
        self.bridge, self.thread = bridge, thread
        self.msg_id = new_id()
        self.text = ""
        self.refused: List[str] = []
        self.tools: deque = deque(maxlen=MAX_ACTIVITY)
        self.acted = False
        self.sent = ""  # last text relayed
        self.last_edit = 0.0
        self.since = int(time.time() * 1000)
        self.started = time.monotonic()
        self.pending_status: Optional[str] = None
        self.late: Optional[asyncio.Task] = None

    def reply(self) -> str:
        notes = "\n".join(f"_{REFUSED.format(title=t)}_" for t in self.refused)
        return (self.text.strip() + ("\n\n" + notes if notes else "")).strip()

    async def flush(self, force: bool = False) -> None:
        body = self.reply()[:MAX_TEXT]
        if not body or body == self.sent:
            return
        if not force and time.monotonic() - self.last_edit < EDIT_EVERY_S:
            return
        self.sent, self.last_edit = body, time.monotonic()
        await self.bridge.send({"type": "message.final", "thread": self.thread, "msg_id": self.msg_id, "text": body,
                                "rev": self.bridge.next_rev()})

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
                                "since": self.since, **({"recent": list(self.tools)} if self.tools else {})})

    async def on_update(self, u: Dict[str, Any]) -> None:
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = u.get("content") or {}
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                self.text += c["text"]
                await self.flush()
        elif kind == "tool_call":
            self.acted = True
            title = str(u.get("title") or u.get("kind") or "tool")[:MAX_STATUS]
            self.tools.append(title)
            await self.status(title)

    async def on_permission(self, p: Dict[str, Any]) -> None:
        tc = p.get("toolCall") if isinstance(p.get("toolCall"), dict) else {}
        title = str(tc.get("title") or tc.get("kind") or "a tool call")[:MAX_STATUS]
        logger.warning("permission refused in thread %s: %s", self.thread, title)
        self.refused.append(title)
        await self.flush(force=True)


class Bridge:
    def __init__(self, env: Dict[str, str], command: List[str], cwd: str, state_path: Optional[str] = None):
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
        self.connected = asyncio.Event()

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
                                     headers={"Authorization": f"Basic {basic}"},
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
        self.session = aiohttp.ClientSession()
        backoff = 0
        try:
            while self.running:
                opened = time.monotonic()
                try:
                    token = await self.access_token()
                    self.ws = await self.session.ws_connect(self.ws_url, protocols=(PROTOCOL, token), heartbeat=None,
                                                            timeout=aiohttp.ClientWSTimeout(ws_close=10))
                    if self.ws.protocol != PROTOCOL:
                        raise RuntimeError(f"hub did not accept the {PROTOCOL} subprotocol")
                    await self.send({"type": "subscribe"})
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
        elif kind == "error":
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
        self._spawn(self.run_turn(thread, frame))

    async def _session_for(self, thread: str) -> str:
        await self.agent.ensure_started()
        if self.agent.proc.pid != self.agent_pid:  # a new process: it knows none of the sessions yet
            self.agent_pid, self.loaded = self.agent.proc.pid, set()
        sid = self.sessions.get(thread)
        if sid and (sid in self.loaded or await self.agent.load_session(sid)):
            self.loaded.add(sid)
            return sid
        sid = await self.agent.new_session()
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
                reason = await self.agent.prompt(sid, prompt_text(frame), turn.on_update, turn.on_permission)
            except (AcpError, OSError) as e:
                logger.warning("turn failed in %s: %s", thread, e)
                if self.agent.stderr_tail:
                    logger.warning("agent stderr (last lines): %s", self.agent.stderr_tail[-800:])
                reason = None
                turn.text += ("\n\n" if turn.text else "") + AGENT_DOWN.format(why=type(e).__name__ if isinstance(e, OSError) else str(e)[:200])
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
