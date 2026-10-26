"""Minimal ACP client (Agent Client Protocol, https://agentclientprotocol.com, protocol version 1).

Spawns the agent command (`kiro-cli acp`, or any ACP agent), speaks newline-delimited JSON-RPC 2.0 on its
stdin/stdout. Only the standard methods are used: initialize, session/new, session/load, session/prompt,
session/cancel; agent -> client: session/update (routed to the turn of that session) and session/request_permission.

Security: the client advertises no file system and no terminal capability, so the agent has no reason to ask this
process to read, write or run anything; any such request is refused (-32601). A permission request is ALWAYS answered
with the agent's own reject option (or `cancelled` when it offers none): what the agent may do unattended is decided
by its own configuration (Kiro: the agent config's allowed tools), never by the bridge. Never `--trust-all-tools`.
"""

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import __version__

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
LINE_LIMIT = 16 * 1024 * 1024  # one JSON-RPC message per line; tool outputs can be large
STDERR_KEEP = 4000


class AcpError(Exception):
    """A failed request, or the agent process gone. The message never carries agent output (it may hold secrets)."""


Update = Callable[[Dict[str, Any]], Awaitable[None]]
Permission = Callable[[Dict[str, Any]], Awaitable[None]]


def reject_option(options: Any) -> Dict[str, Any]:
    """The permission answer: the agent's reject_once option, else reject_always, else `cancelled`.
    Never an option the agent did not offer (ACP: the options list is authoritative)."""
    opts = [o for o in options if isinstance(o, dict)] if isinstance(options, list) else []
    for kind in ("reject_once", "reject_always"):
        for o in opts:
            if o.get("kind") == kind and isinstance(o.get("optionId"), str):
                return {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


class AcpAgent:
    """One agent process, several sessions. `on_update(sessionId)` / `on_permission(sessionId)` are looked up per
    session (the turn running in it); updates of a session without a turn (replay of session/load) are dropped."""

    def __init__(self, command: List[str], cwd: str):
        self.command, self.cwd = command, cwd
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.capabilities: Dict[str, Any] = {}
        self.info: Dict[str, Any] = {}
        self._next = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._updates: Dict[str, Update] = {}
        self._permissions: Dict[str, Permission] = {}
        self._reader: Optional[asyncio.Task] = None
        self._stderr: Optional[asyncio.Task] = None
        self.stderr_tail = ""
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self.alive:
                return
            self._fail_pending("the agent process restarted")
            self.proc = await asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT)
            self.stderr_tail = ""
            self._reader = asyncio.create_task(self._read(self.proc))
            self._stderr = asyncio.create_task(self._read_stderr(self.proc))
            res = await self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "briefling-acp-bridge", "version": __version__},
            })
            self.capabilities = res.get("agentCapabilities") or {} if isinstance(res, dict) else {}
            self.info = res.get("agentInfo") or {} if isinstance(res, dict) else {}
            logger.info("acp agent started: %s %s", self.info.get("name", "?"), self.info.get("version", ""))

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        for t in (self._reader, self._stderr):
            if t:
                t.cancel()

    # -- JSON-RPC ---------------------------------------------------------------

    async def _write(self, obj: Dict[str, Any]) -> None:
        if not self.alive or self.proc.stdin is None:
            raise AcpError("the agent process is not running")
        async with self._write_lock:
            try:
                self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", **obj}) + "\n").encode())
                await self.proc.stdin.drain()
            except (ConnectionError, BrokenPipeError):
                raise AcpError("the agent process is not running") from None

    async def request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        self._next += 1
        rid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"id": rid, "method": method, "params": params})
            return await (asyncio.wait_for(fut, timeout) if timeout else fut)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    def _fail_pending(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpError(why))
        self._pending.clear()

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self.stderr_tail = (self.stderr_tail + line.decode(errors="replace"))[-STDERR_KEEP:]
            logger.debug("agent stderr: %s", line.decode(errors="replace").rstrip())

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # not JSON-RPC (a stray log line): ignored
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        except Exception:
            logger.exception("acp reader stopped")
        finally:
            await proc.wait()
            await asyncio.sleep(0)  # let the stderr reader drain
            logger.warning("acp agent exited (code %s)", proc.returncode)
            self._fail_pending(f"the agent process exited (code {proc.returncode})")

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        method, rid = msg.get("method"), msg.get("id")
        if method is None:  # a response
            fut = self._pending.get(rid) if isinstance(rid, int) else None
            if fut and not fut.done():
                if "error" in msg:
                    err = msg.get("error") or {}
                    fut.set_exception(AcpError(f"{err.get('code', '?')}: {str(err.get('message', ''))[:200]}"))
                else:
                    fut.set_result(msg.get("result"))
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        sid = params.get("sessionId")
        if method == "session/update":
            cb = self._updates.get(sid) if isinstance(sid, str) else None
            if cb and isinstance(params.get("update"), dict):
                await cb(params["update"])
            return
        if rid is None:  # other notifications (Kiro extensions...): ignored, but an MCP server ready is worth a line
            if method == "_kiro.dev/mcp/server_initialized":
                logger.info("agent: MCP server ready: %s", str(params.get("serverName") or params.get("name") or params)[:120])
            return
        if method == "session/request_permission":
            cb = self._permissions.get(sid) if isinstance(sid, str) else None
            if cb:
                await cb(params)
            await self._write({"id": rid, "result": reject_option(params.get("options"))})
            return
        # fs/*, terminal/* (not advertised), extensions: refused
        await self._write({"id": rid, "error": {"code": -32601, "message": f"{method} is not supported by this client"}})

    # -- sessions -----------------------------------------------------------------

    async def new_session(self, mcp_servers: Optional[List[Dict[str, Any]]] = None) -> str:
        res = await self.request("session/new", {"cwd": self.cwd, "mcpServers": mcp_servers or []}, timeout=120)
        sid = res.get("sessionId") if isinstance(res, dict) else None
        if not isinstance(sid, str) or not sid:
            raise AcpError("session/new returned no sessionId")
        return sid

    async def load_session(self, sid: str, mcp_servers: Optional[List[Dict[str, Any]]] = None) -> bool:
        """Resume a session after a restart, when the agent supports it. Its replayed updates are dropped (no turn)."""
        if not self.capabilities.get("loadSession"):
            return False
        try:
            await self.request("session/load", {"sessionId": sid, "cwd": self.cwd, "mcpServers": mcp_servers or []},
                               timeout=120)
            return True
        except (AcpError, asyncio.TimeoutError):
            return False

    async def prompt(self, sid: str, text: str, on_update: Update, on_permission: Permission) -> str:
        """Run one turn; returns the stop reason (end_turn, cancelled, refusal, max_tokens...)."""
        self._updates[sid], self._permissions[sid] = on_update, on_permission
        try:
            res = await self.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})
        finally:
            self._updates.pop(sid, None)
            self._permissions.pop(sid, None)
        reason = res.get("stopReason") if isinstance(res, dict) else None
        return reason if isinstance(reason, str) else "end_turn"

    async def cancel(self, sid: str) -> None:
        try:
            await self.notify("session/cancel", {"sessionId": sid})
        except AcpError:
            pass
