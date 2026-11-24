"""`briefling-acp mcp`: the MCP server (stdio) that gives the agent briefling's tools.

The agent starts it as one of its MCP servers: the bridge declares it in `session/new` (one per conversation, with
that conversation's token in its environment), or, when the agent ignores the servers of `session/new`, the operator
declares it once in the agent's own config (no token: the bridge then ties each call to the only turn running).

It only relays: each `tools/call` goes to the bridge over its Unix socket (in a directory only this user can open) as
one JSON line {token, tool, args}; on Windows the socket's path holds instead an endpoint file {port, key} (same
private directory): the line goes to 127.0.0.1:port and carries the key; the bridge checks the token, the running turn and the arguments, sends the frame to
the hub and answers with the tool result. Standard library only, no MCP SDK: the server needs `initialize`,
`tools/list`, `tools/call` and `ping`, newline-delimited JSON-RPC 2.0 like ACP.
"""

import io
import json
import os
import socket
import sys
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from .tools import mcp_tools

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # MCP revisions this server speaks (tools only)
CALL_TIMEOUT_S = 90.0  # the bridge answers within its own hub timeout (20 s); margin for a slow machine
SOCKET_ENV, TOKEN_ENV = "BRIEFLING_ACP_SOCKET", "BRIEFLING_ACP_TOKEN"


def call_bridge(path: str, token: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """One tool call to the bridge: {ok, ...} as the bridge returns it, or a local failure."""
    return _ask_bridge(path, {"token": token, "tool": tool, "args": args})


def mails_of(path: str, token: str) -> bool:
    """Does this conversation get the mail tools (its space's « exchanges » module)? Asked to the bridge at tools/list;
    an older bridge or none reachable: yes (the hub refuses them all the same when the module is off)."""
    res = _ask_bridge(path, {"token": token, "list": True})
    return res.get("mails") is not False


def _ask_bridge(path: str, req: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if os.path.isfile(path):  # the bridge's TCP endpoint (Windows): a Unix socket is not a regular file
            with open(path) as f:
                ep = json.load(f)
            family, addr, req["key"] = socket.AF_INET, ("127.0.0.1", int(ep["port"])), str(ep["key"])
        else:
            family, addr = socket.AF_UNIX, path
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(CALL_TIMEOUT_S)
            s.connect(addr)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        res = json.loads(buf)
        return res if isinstance(res, dict) else {"ok": False, "error": "bridge_error", "detail": "unexpected answer"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:  # AttributeError: no AF_UNIX (Windows)
        return {"ok": False, "error": "bridge_unreachable",
                "detail": f"the briefling bridge is not reachable ({type(e).__name__}): is `briefling-acp` running?"}


def _route(msg: Dict[str, Any], mails: Callable[[], bool] = lambda: True
           ) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[str, Dict[str, Any]]]]:
    """(answer, None), or (None, (tool, args)) for a valid tools/call; (None, None) for a notification. mails(): does
    tools/list include the mail tools (asked only for tools/list)."""
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None, None  # notifications/initialized, notifications/cancelled...
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"id": rid, "result": {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "briefling", "version": _version()},
            "instructions": "briefling's tools for this conversation (cards, turn outcome, actions, project memory).",
        }}, None
    if method == "ping":
        return {"id": rid, "result": {}}, None
    if method == "tools/list":
        return {"id": rid, "result": {"tools": mcp_tools(mails())}}, None
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in {t["name"] for t in mcp_tools()}:
            return {"id": rid, "error": {"code": -32602, "message": f"unknown tool: {str(name)[:60]}"}}, None
        return None, (name, args if isinstance(args, dict) else {})
    return {"id": rid, "error": {"code": -32601, "message": f"{str(method)[:60]} is not supported"}}, None


def _call_result(rid: Any, res: Dict[str, Any]) -> Dict[str, Any]:
    if res.get("ok") is True and isinstance(res.get("content"), list):  # attachment_get: MCP text / image content as is
        return {"id": rid, "result": {"content": res["content"], "isError": False}}
    return {"id": rid, "result": {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                                  "isError": res.get("ok") is not True}}


def handle(msg: Dict[str, Any], path: str, token: str) -> Optional[Dict[str, Any]]:
    """The JSON-RPC answer to one message (None for a notification); a tool call goes to the bridge's socket."""
    out, call = _route(msg, lambda: mails_of(path, token))
    if call is None:
        return out
    return _call_result(msg.get("id"), call_bridge(path, token, *call))


async def handle_async(msg: Dict[str, Any], call: Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]],
                       mails: Callable[[], bool] = lambda: True) -> Optional[Dict[str, Any]]:
    """Same answers as `handle`, a tool call going to `call(tool, args)` (the HTTP server, inside the bridge)."""
    out, c = _route(msg, mails)
    if c is None:
        return out
    return _call_result(msg.get("id"), await call(*c))


def _version() -> str:
    from . import __version__
    return __version__


def serve(path: str, token: str, stdin=None, stdout=None) -> int:
    # MCP stdio is UTF-8 JSON; Python's console default on Windows is the ANSI code page (cp1252: « à » read as « Ã »).
    stdin = stdin or io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    stdout = stdout or io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    for line in stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        out = handle(msg, path, token)
        if out is not None:
            stdout.write(json.dumps({"jsonrpc": "2.0", **out}) + "\n")
            stdout.flush()
    return 0


def main(socket_path: str) -> int:
    """Entry of `briefling-acp mcp`: the socket comes from the bridge (env) or from the credentials file's place."""
    return serve(os.environ.get(SOCKET_ENV) or socket_path, os.environ.get(TOKEN_ENV, ""))
