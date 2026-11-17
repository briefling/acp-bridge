"""briefling's MCP server over HTTP, served by the bridge itself, for an agent that cannot start a local command.

An agent in a microVM (Docker Sandboxes, `sbx`) cannot run `briefling-acp mcp` on the host nor open the bridge's Unix
socket. With `--mcp-port`, the bridge also serves the same tools as an MCP "Streamable HTTP" server and declares it in
`session/new|load` as an `http` server (when the agent advertises `mcpCapabilities.http`), with the conversation's
token in an `Authorization: Bearer` header, so each call stays tied to its conversation, as with the stdio server.

Surface: it listens on 127.0.0.1 only (a sandbox reaches it through its host proxy, `host.docker.internal`, which
connects to the host's localhost); every request needs a token the bridge issued for one conversation (no token =
401: unlike the stdio server, there is no "only turn running" fallback, since any local process can open a port);
a request carrying an `Origin` header (a browser page) is refused; one POST endpoint, JSON answers only (no SSE
stream, no server-initiated message: GET and DELETE answer 405, which the transport allows).
"""

import json
from typing import Any, Awaitable, Callable, Dict, Optional

from aiohttp import web

from . import mcp

PATH = "/mcp"
MAX_BODY = 1 << 20

ThreadOf = Callable[[str], Optional[str]]
ToolCall = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _bearer(req: web.Request) -> str:
    auth = req.headers.get("Authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def app(thread_of: ThreadOf, tool_call: ToolCall) -> web.Application:
    """The aiohttp app: thread_of(token) -> the conversation of a known token; tool_call({token, tool, args})."""

    async def post(req: web.Request) -> web.StreamResponse:
        if "Origin" in req.headers:
            return web.json_response({"error": "forbidden"}, status=403)
        token = _bearer(req)
        if not token or thread_of(token) is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            msg = json.loads(await req.read())
        except ValueError:
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                                     status=400)
        if not isinstance(msg, dict):  # JSON-RPC batches left MCP in 2025-06-18
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "one request per POST"}},
                                     status=400)
        if msg.get("id") is None:  # notification or response: accepted, nothing to answer
            return web.Response(status=202)

        async def call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
            return await tool_call({"token": token, "tool": tool, "args": args})

        out = await mcp.handle_async(msg, call)
        return web.json_response({"jsonrpc": "2.0", **out})

    async def not_allowed(_req: web.Request) -> web.StreamResponse:
        return web.Response(status=405, headers={"Allow": "POST"})

    a = web.Application(client_max_size=MAX_BODY)
    a.router.add_post(PATH, post)
    a.router.add_get(PATH, not_allowed)
    a.router.add_delete(PATH, not_allowed)
    return a
