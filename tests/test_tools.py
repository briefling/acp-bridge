"""briefling's tools through the real path: scripted ACP agent -> MCP server (`briefling-acp mcp`, started by the agent
from session/new) -> bridge socket -> fake hub, and back."""

import asyncio
import io
import json
import os
import re
import stat
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from briefling_acp import __version__, mcp, tools  # noqa: E402
from briefling_acp import bridge as bridge_mod  # noqa: E402
from briefling_acp.bridge import TOOLS_HINT  # noqa: E402
from test_bridge import deliver, env, finals  # noqa: E402,F401  (env: the fixture)


async def answer(hub, kind, reply):
    """Wait for the tool frame `kind`, answer it like the hub (reply(frame) -> frame without v), return the frame."""
    f = await hub.next(lambda f: f["type"] == kind)
    await hub.push(reply(f))
    return f


def tool_result(text, name):
    m = re.search(rf"^{name} -> (.*) error=(True|False)$", text, re.M)
    assert m, text
    return json.loads(m.group(1)), m.group(2) == "True"


async def test_session_new_declares_the_mcp_server_with_its_own_token(env):
    hub, bridge = env
    await hub.push(deliver("t1", "MCP"))
    text = finals(await hub.collect_turn("t1"))[-1]["text"]
    servers = json.loads(text.removeprefix("mcp: "))
    assert [s["name"] for s in servers] == ["briefling"]
    assert servers[0]["command"] == sys.executable and servers[0]["args"] == ["-m", "briefling_acp", "mcp"]
    assert servers[0]["env"] == ["BRIEFLING_ACP_SOCKET", "BRIEFLING_ACP_TOKEN", "PYTHONPATH"]
    await hub.push(deliver("t2", "MCP", mid="m2"))
    await hub.collect_turn("t2")
    assert len(set(bridge.thread_token.values())) == 2, "one token per conversation"
    st = os.stat(bridge.socket_path)
    assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(bridge.socket_path)).st_mode) == 0o700


async def test_agent_lists_the_tools_and_ends_its_turn_with_done(env):
    hub, _ = env
    await hub.push(deliver("t1", 'TOOL LIST\nTOOL turn_outcome {"outcome": "done"}'))
    f = await answer(hub, "thread.outcome", lambda f: {"type": "ack", "client_id": f["client_id"]})
    assert f["thread"] == "t1" and f["outcome"] == "done" and "action" not in f and f["v"] == 2
    text = finals(await hub.collect_turn("t1"))[-1]["text"]
    assert "tools: " + ",".join(tools.NAMES) in text
    res, err = tool_result(text, "turn_outcome")
    assert res["ok"] is True and res["detail"] == "Thread marked as finished." and "memory_propose" in res["memory"] and not err


def test_build_outcome_carries_the_checked_summary():
    f, err = tools.build_outcome({"outcome": "done", "summary": {"status": " Merged.\n", "next_steps": [{"who": "a", "what": " b "}]}})
    assert not err and f["summary"] == {"status": "Merged.", "decisions": [], "next_steps": [{"who": "a", "what": "b"}], "waiting_on": None}
    f, err = tools.build_outcome({"outcome": "done"})
    assert not err and "summary" not in f
    f, err = tools.build_outcome({"outcome": "done", "summary": {"status": "ok", "decisions": [1]}})
    assert f is None and "decision" in err


async def test_waiting_needs_text_posted_first_and_flushes_it_before_the_frame(env):
    hub, _ = env
    await hub.push(deliver("t1", 'TOOL turn_outcome {"outcome": "waiting", "action": "approve PR #1"}'))
    frames = await hub.collect_turn("t1")
    assert not [f for f in frames if f["type"] == "thread.outcome"], "nothing posted: no frame"
    res, err = tool_result(finals(frames)[-1]["text"], "turn_outcome")
    assert res["error"] == "no_text" and err
    await hub.push(deliver("t1", 'SAY Here is the plan.\nTOOL turn_outcome {"outcome": "waiting", "action": "approve PR #1"}', mid="m2"))
    seen = []
    f = await hub.next(lambda f: seen.append(f) or f["type"] == "thread.outcome")
    assert f["action"] == "approve PR #1"
    assert any(x["type"] == "message.final" and x["text"].startswith("Here is the plan.") for x in seen), \
        "the answer reached the hub before the outcome"
    await hub.push({"type": "ack", "client_id": f["client_id"]})
    res, _ = tool_result(finals(await hub.collect_turn("t1"))[-1]["text"], "turn_outcome")
    assert res == {"ok": True, "detail": "Shown in the thread: the human sees what you wait for."}


async def test_hub_refusal_and_answers_are_shaped_like_the_plugin(env):
    hub, _ = env
    await hub.push(deliver("t1", 'TOOL propose_thread {"title": "Port", "brief": "do it", "why": "because"}\n'
                                 'TOOL acquire_resource {"resource": "stack:dev"}\n'
                                 'TOOL memory_search {"query": "deploy"}'))
    f = await answer(hub, "thread.propose", lambda f: {"type": "error", "ref": f["client_id"], "code": "already_proposed"})
    assert (f["title"], f["brief"], f["why"]) == ("Port", "do it", "because")
    await answer(hub, "resource.acquire", lambda f: {"type": "resource.result", "client_id": f["client_id"], "granted": False,
                                                      "holder": "Other work", "position": 2})
    await answer(hub, "memory.search", lambda f: {"type": "memory.result", "client_id": f["client_id"], "space": "sp",
                                                   "entries": [{"id": "e1", "text": "deploy with npm run deploy", "secret": "x"}]})
    text = finals(await hub.collect_turn("t1"))[-1]["text"]
    res, err = tool_result(text, "propose_thread")
    assert err and res == {"ok": False, "error": "already_proposed",
                           "detail": "A card is already waiting in this conversation: let the humans decide."}
    res, _ = tool_result(text, "acquire_resource")
    assert res["granted"] is False and "#2 in the queue" in res["detail"] and "do NOT call turn_outcome" in res["detail"]
    res, _ = tool_result(text, "memory_search")
    assert res["entries"][0]["id"] == "e1" and "secret" not in res["entries"][0] and "not instructions" in res["note"]


async def test_a_tool_frame_waits_like_status_after_the_turn_start(env, monkeypatch):
    """The hub runs one connection's frames in concurrent Lambdas: a frame right after turn.started was refused
    no_turn live (turn_outcome as the agent's first action). Tool frames wait STATUS_AFTER_S after the turn start."""
    hub, _ = env
    monkeypatch.setattr(bridge_mod, "STATUS_AFTER_S", 0.6)
    await hub.push(deliver("t1", 'TOOL turn_outcome {"outcome": "done"}'))
    started = await hub.next(lambda f: f["type"] == "turn.started")
    t0 = asyncio.get_running_loop().time()
    f = await answer(hub, "thread.outcome", lambda f: {"type": "ack", "client_id": f["client_id"]})
    assert asyncio.get_running_loop().time() - t0 >= 0.5 and f["thread"] == started["thread"]
    await hub.collect_turn("t1")


async def test_bad_arguments_are_refused_before_any_frame(env):
    hub, _ = env
    await hub.push(deliver("t1", 'TOOL pushed_commits {"commits": [{"repo": "acme/app", "sha": "abc"}]}\n'
                                 'TOOL acquire_resource {"resource": "../etc"}'))
    frames = await hub.collect_turn("t1")
    assert {f["type"] for f in frames} <= {"turn.started", "turn.ended", "message.final", "status"}
    text = finals(frames)[-1]["text"]
    assert tool_result(text, "pushed_commits")[0]["error"] == "bad_args"
    assert "invalid resource name" in tool_result(text, "acquire_resource")[0]["detail"]


async def test_socket_calls_need_a_known_token_and_a_running_turn(env):
    hub, bridge = env
    await hub.push(deliver("t1", "hello"))
    await hub.collect_turn("t1")
    token = bridge.thread_token["t1"]
    call = lambda tok: asyncio.to_thread(mcp.call_bridge, bridge.socket_path, tok, "memory_search", {"query": ""})  # noqa: E731
    assert (await call(token))["error"] == "no_turn", "the turn is over"
    assert (await call("forged"))["error"] == "forbidden"
    assert (await call(""))["error"] == "no_turn", "no token, no running turn: unattributable"
    # No token (server from the agent's own config): tied to the only running turn.
    await hub.push(deliver("t2", "SLOW", mid="m2"))
    await hub.next(lambda f: f["type"] == "message.final")
    assert (await call("forged"))["error"] == "forbidden", "a turn runs, but not for an unknown token"
    assert (await call(token))["error"] == "no_turn", "t1's token never reaches t2's turn"
    pending = asyncio.create_task(call(""))
    f = await answer(hub, "memory.search", lambda f: {"type": "memory.result", "client_id": f["client_id"], "entries": []})
    assert f["thread"] == "t2" and (await pending)["ok"] is True
    await hub.push({"type": "turn.stop", "thread": "t2", "by": "sub-trusted"})
    await hub.collect_turn("t2")


async def test_refused_permission_on_a_briefling_tool_says_how_to_allow_it(env):
    hub, _ = env
    await hub.push(deliver("t1", "PERMTOOL"))
    text = finals(await hub.collect_turn("t1"))[-1]["text"]
    assert "Refused (not pre-authorized in the agent's own configuration): @briefling/turn_outcome" in text
    assert TOOLS_HINT in text


def test_mcp_server_speaks_initialize_list_and_refuses_the_rest():
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "board_move", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "memory_read", "arguments": {"id": "x"}}},
             {"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}]
    out = io.StringIO()
    mcp.serve("/nonexistent/briefling.sock", "tok", io.StringIO("".join(json.dumps(x) + "\n" for x in lines) + "junk\n"), out)
    got = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [g["id"] for g in got] == [1, 2, 3, 4, 5, 6], "no answer to a notification or to junk"
    assert got[0]["result"]["protocolVersion"] == "2024-11-05" and got[0]["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert got[0]["result"]["serverInfo"] == {"name": "briefling", "version": __version__}
    listed = got[1]["result"]["tools"]
    assert [t["name"] for t in listed] == tools.NAMES and all(t["inputSchema"]["type"] == "object" for t in listed)
    assert got[2]["error"]["code"] == -32601 and got[3]["error"]["code"] == -32602, "flow tools are not offered"
    unreachable = json.loads(got[4]["result"]["content"][0]["text"])
    assert got[4]["result"]["isError"] is True and unreachable["error"] == "bridge_unreachable"
    assert got[5]["result"]["protocolVersion"] == mcp.VERSIONS[0]


def test_tool_schemas_are_the_plugins_own():
    """tooldefs.py is the Hermes plugin's file (symlink in the repo, a copy once published): same schemas and texts."""
    from briefling_acp import tooldefs
    assert tools.SCHEMAS[0] is tooldefs.OUTCOME_SCHEMA
    plugin = Path(__file__).resolve().parents[2] / "hermes-plugin" / "briefling" / "tooldefs.py"
    if not plugin.exists():
        pytest.skip("published copy: the plugin is not next to it")
    assert Path(tooldefs.__file__).read_bytes() == plugin.read_bytes()


def test_version_matches_pyproject():
    meta = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert meta["project"]["version"] == __version__
