"""briefling's tools over HTTP (`--mcp-port`): scripted ACP agent advertising mcpCapabilities.http -> the bridge's own
HTTP MCP server (token in a header) -> fake hub, and back; plus the server's gates."""

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from briefling_acp import __main__ as cli  # noqa: E402
from briefling_acp import tools  # noqa: E402
from briefling_acp.bridge import Bridge  # noqa: E402
from test_bridge import FAKE, deliver, env, finals, running  # noqa: E402,F401  (env: the fixture)
from test_tools import answer, tool_result  # noqa: E402


@pytest.fixture
async def http_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MCP_HTTP", "1")
    async with running(tmp_path, mcp_port=0, mcp_host="host.docker.internal") as hb:
        yield hb


async def test_session_declares_the_http_server_with_the_conversations_token(http_env):
    hub, bridge = http_env
    await hub.push(deliver("t1", "MCP"))
    servers = json.loads(finals(await hub.collect_turn("t1"))[-1]["text"].removeprefix("mcp: "))
    token = bridge.thread_token["t1"]
    assert servers == [{"type": "http", "name": "briefling", "url": f"http://host.docker.internal:{bridge.mcp_port}/mcp",
                        "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}]
    assert token not in servers[0]["url"], "the token rides in a header, never in the URL"
    sockets = next(iter(bridge.mcp_runner.sites))._server.sockets
    assert {s.getsockname()[0] for s in sockets} == {"127.0.0.1"}, "loopback only, whatever --mcp-host says"


async def test_agent_lists_the_tools_and_calls_one_over_http(http_env):
    hub, _ = http_env
    await hub.push(deliver("t1", 'TOOL LIST\nTOOL turn_outcome {"outcome": "done"}'))
    f = await answer(hub, "thread.outcome", lambda f: {"type": "ack", "client_id": f["client_id"]})
    assert f["thread"] == "t1" and f["outcome"] == "done"
    text = finals(await hub.collect_turn("t1"))[-1]["text"]
    assert "tools: " + ",".join(tools.NAMES) in text
    res, err = tool_result(text, "turn_outcome")
    assert res["ok"] is True and not err


async def test_agent_without_http_support_keeps_the_stdio_server(tmp_path):
    async with running(tmp_path, mcp_port=0) as (hub, _):
        await hub.push(deliver("t1", "MCP"))
        servers = json.loads(finals(await hub.collect_turn("t1"))[-1]["text"].removeprefix("mcp: "))
        assert "type" not in servers[0] and servers[0]["args"] == ["-m", "briefling_acp", "mcp"]


async def test_no_port_no_http_server(env):
    _, bridge = env
    assert bridge.mcp_runner is None


async def test_http_server_needs_a_known_token_no_origin_and_post(http_env):
    hub, bridge = http_env
    await hub.push(deliver("t1", "hello"))
    await hub.collect_turn("t1")
    token = bridge.thread_token["t1"]
    url = f"http://127.0.0.1:{bridge.mcp_port}/mcp"
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": ""}}}
    async with aiohttp.ClientSession() as s:
        async def post(body, **headers):
            async with s.post(url, data=json.dumps(body), headers=headers) as r:
                return r.status, (await r.json() if r.content_type == "application/json" else None)
        assert (await post(call))[0] == 401, "no token: no fallback to the running turn over a TCP port"
        assert (await post(call, Authorization="Bearer forged"))[0] == 401
        assert (await post(call, Authorization=f"Bearer {token}", Origin="http://evil.example"))[0] == 403
        st, body = await post({"jsonrpc": "2.0", "method": "notifications/initialized"}, Authorization=f"Bearer {token}")
        assert st == 202 and body is None
        assert (await post([call], Authorization=f"Bearer {token}"))[0] == 400, "no batch"
        st, body = await post(call, Authorization=f"Bearer {token}")
        assert st == 200 and body["id"] == 7 and body["result"]["isError"] is True
        assert json.loads(body["result"]["content"][0]["text"])["error"] == "no_turn", "the turn is over"
        async with s.get(url, headers={"Authorization": f"Bearer {token}"}) as r:
            assert r.status == 405, "no server-initiated stream"
        # During t2's turn, t1's token still never reaches t2.
        await hub.push(deliver("t2", "SLOW", mid="m2"))
        await hub.next(lambda f: f["type"] == "message.final")
        st, body = await post(call, Authorization=f"Bearer {token}")
        assert json.loads(body["result"]["content"][0]["text"])["error"] == "no_turn"
        await hub.push({"type": "turn.stop", "thread": "t2", "by": "sub-trusted"})
        await hub.collect_turn("t2")


async def test_port_in_use_stops_the_bridge_with_a_clear_log(tmp_path, caplog):
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    taken = blocker.sockets[0].getsockname()[1]
    conf = {"BRIEFLING_WS_URL": "ws://127.0.0.1:9/ws", "BRIEFLING_TOKEN_URL": "http://127.0.0.1:9/token",
            "BRIEFLING_CLIENT_ID": "cid", "BRIEFLING_CLIENT_SECRET": "secret", "BRIEFLING_ALLOWED_USERS": "x"}
    bridge = Bridge(conf, FAKE, str(tmp_path), mcp_port=taken)
    try:
        with pytest.raises(SystemExit):
            await bridge.run()
        assert f"cannot serve briefling's MCP tools on 127.0.0.1:{taken}" in caplog.text
        assert not bridge.agent.alive and bridge.socket_server is None, "nothing left running"
    finally:
        blocker.close()


def test_cli_checks_the_mcp_options(capsys):
    for argv in (["--mcp-port", "0"], ["--mcp-port", "x"], ["--mcp-port", "8765", "--mcp-host", "a b"],
                 ["--mcp-host", "host.docker.internal"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2, argv
    assert "--mcp-host needs --mcp-port" in capsys.readouterr().err
