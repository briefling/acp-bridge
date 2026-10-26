"""`briefling-acp mcp`: the MCP server (stdio) that gives the agent briefling's tools.

The agent starts it as one of its MCP servers: the bridge declares it in `session/new` (one per conversation, with
that conversation's token in its environment), or, when the agent ignores the servers of `session/new`, the operator
declares it once in the agent's own config (no token: the bridge then ties each call to the only turn running).

It only relays: each `tools/call` goes to the bridge over its Unix socket (in a directory only this user can open) as
one JSON line {token, tool, args}; the bridge checks the token, the running turn and the arguments, sends the frame to
the hub and answers with the tool result. Standard library only, no MCP SDK: the server needs `initialize`,
`tools/list`, `tools/call` and `ping`, newline-delimited JSON-RPC 2.0 like ACP.
"""

import json
import os
import socket
import sys
from typing import Any, Dict, Optional

from .tools import mcp_tools

VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # MCP revisions this server speaks (tools only)
CALL_TIMEOUT_S = 90.0  # the bridge answers within its own hub timeout (20 s); margin for a slow machine
SOCKET_ENV, TOKEN_ENV = "BRIEFLING_ACP_SOCKET", "BRIEFLING_ACP_TOKEN"


def call_bridge(path: str, token: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """One tool call to the bridge: {ok, ...} as the bridge returns it, or a local failure."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(CALL_TIMEOUT_S)
            s.connect(path)
            s.sendall((json.dumps({"token": token, "tool": tool, "args": args}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        res = json.loads(buf)
        return res if isinstance(res, dict) else {"ok": False, "error": "bridge_error", "detail": "unexpected answer"}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": "bridge_unreachable",
                "detail": f"the briefling bridge is not reachable ({type(e).__name__}): is `briefling-acp` running?"}


def handle(msg: Dict[str, Any], path: str, token: str) -> Optional[Dict[str, Any]]:
    """The JSON-RPC answer to one message (None for a notification)."""
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None  # notifications/initialized, notifications/cancelled...
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"id": rid, "result": {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "briefling", "version": _version()},
            "instructions": "briefling's tools for this conversation (cards, turn outcome, shared resources, project memory).",
        }}
    if method == "ping":
        return {"id": rid, "result": {}}
    if method == "tools/list":
        return {"id": rid, "result": {"tools": mcp_tools()}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or name not in {t["name"] for t in mcp_tools()}:
            return {"id": rid, "error": {"code": -32602, "message": f"unknown tool: {str(name)[:60]}"}}
        res = call_bridge(path, token, name, args if isinstance(args, dict) else {})
        return {"id": rid, "result": {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                                      "isError": res.get("ok") is not True}}
    return {"id": rid, "error": {"code": -32601, "message": f"{str(method)[:60]} is not supported"}}


def _version() -> str:
    from . import __version__
    return __version__


def serve(path: str, token: str, stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
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
