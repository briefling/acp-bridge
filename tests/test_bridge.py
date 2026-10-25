"""The bridge against a fake hub (aiohttp on 127.0.0.1: token endpoint + WebSocket) and a scripted ACP agent."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from briefling_acp import __main__ as cli  # noqa: E402
from briefling_acp.acp import reject_option  # noqa: E402
from briefling_acp import bridge as bridge_mod  # noqa: E402
from briefling_acp.bridge import Bridge, prompt_text  # noqa: E402

FAKE = [sys.executable, str(Path(__file__).with_name("fake_agent.py"))]
TRUSTED, OTHER = "sub-trusted", "sub-other"


class Hub:
    def __init__(self):
        self.got: asyncio.Queue = asyncio.Queue()
        self.ws = None
        self.tokens = 0

    async def token(self, req):
        assert req.headers["Authorization"].startswith("Basic ")
        self.tokens += 1
        return web.json_response({"access_token": "tok", "expires_in": 3600})

    async def wsh(self, req):
        ws = web.WebSocketResponse(protocols=("briefling.v2",))
        await ws.prepare(req)
        self.ws = ws
        async for m in ws:
            await self.got.put(json.loads(m.data))
        return ws

    async def push(self, frame):
        await self.ws.send_str(json.dumps({"v": 2, **frame}))

    async def next(self, pred=lambda f: True, timeout=10.0):
        async with asyncio.timeout(timeout):
            while True:
                f = await self.got.get()
                if pred(f):
                    return f

    async def collect_turn(self, thread, timeout=10.0):
        """Every frame of `thread` up to its turn.ended."""
        out = []
        async with asyncio.timeout(timeout):
            while True:
                f = await self.got.get()
                if f.get("thread") == thread:
                    out.append(f)
                    if f["type"] == "turn.ended":
                        return out


@pytest.fixture
async def env(tmp_path):
    hub = Hub()
    app = web.Application()
    app.router.add_post("/token", hub.token)
    app.router.add_get("/ws", hub.wsh)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    conf = {"BRIEFLING_WS_URL": f"ws://127.0.0.1:{port}/ws", "BRIEFLING_TOKEN_URL": f"http://127.0.0.1:{port}/token",
            "BRIEFLING_CLIENT_ID": "cid", "BRIEFLING_CLIENT_SECRET": "secret", "BRIEFLING_ALLOWED_USERS": f"{TRUSTED},x"}
    bridge = Bridge(conf, FAKE, str(tmp_path), state_path=str(tmp_path / "s.json"))
    task = asyncio.create_task(bridge.run())
    assert (await hub.next())["type"] == "subscribe"
    yield hub, bridge
    bridge.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await runner.cleanup()


def deliver(thread, text, author=TRUSTED, mid="m1", kind="thread"):
    return {"type": "deliver", "thread": thread, "space": "sp", "agent": "kiro", "author_name": "Ada",
            "message": {"id": mid, "thread": thread, "author": {"kind": "human", "id": author}, "type": "text", "text": text, "at": 1},
            "context": {"title": "Port Kiro", "kind": kind, "unseen": []}}


def finals(frames):
    return [f for f in frames if f["type"] == "message.final"]


async def test_turn_streams_one_message_and_reuses_the_session(env, monkeypatch):
    hub, bridge = env
    await hub.push(deliver("t1", "hello there"))
    frames = await hub.collect_turn("t1")
    assert frames[0]["type"] == "turn.started" and frames[-1]["type"] == "turn.ended"
    assert "stopped" not in frames[-1]
    msgs = finals(frames)
    assert len({m["msg_id"] for m in msgs}) == 1, "one message per turn, streamed by edits"
    assert [m["rev"] for m in msgs] == sorted(m["rev"] for m in msgs)
    assert msgs[-1]["text"] == "Echo[s1#1]: hello there"
    status = [f for f in frames if f["type"] == "status"]
    assert not status, "a turn shorter than STATUS_AFTER_S sends no status (the hub could take it before turn.started)"
    assert frames[0]["at"] < frames[-1]["at"]
    await hub.push(deliver("t1", "again", mid="m2"))
    assert finals(await hub.collect_turn("t1"))[-1]["text"] == "Echo[s1#2]: again"
    await hub.push(deliver("t2", "other thread", mid="m3"))
    assert finals(await hub.collect_turn("t2"))[-1]["text"] == "Echo[s2#1]: other thread"
    assert json.load(open(bridge.state_path)) == {"sessions": {"t1": "s1", "t2": "s2"}}
    monkeypatch.setattr(bridge_mod, "STATUS_AFTER_S", 0.0)
    await hub.push(deliver("t3", "with status", mid="m0"))
    status = [f for f in await hub.collect_turn("t3") if f["type"] == "status"]
    assert status and status[0]["text"] == "Reading README.md" and status[0]["recent"] == ["Reading README.md"]


async def test_untrusted_author_and_duplicates_are_dropped(env):
    hub, _ = env
    await hub.push(deliver("t1", "hi", author=OTHER))
    await hub.push(deliver("t1", "hi", author=""))
    with pytest.raises(TimeoutError):
        await hub.next(timeout=1.0)
    await hub.push(deliver("t1", "first", mid="d1"))
    await hub.collect_turn("t1")
    await hub.push(deliver("t1", "first", mid="d1"))  # redelivery of the same message: ignored
    with pytest.raises(TimeoutError):
        await hub.next(timeout=1.0)


async def test_permission_is_always_rejected_with_the_agents_own_option(env):
    hub, _ = env
    await hub.push(deliver("t1", "please PERMISSION"))
    text = finals(await hub.collect_turn("t1"))[-1]["text"]
    assert 'permission answer: {"outcome": {"optionId": "no", "outcome": "selected"}}' in text
    assert "Refused (not pre-authorized in the agent's own configuration): Run rm -rf build" in text


async def test_client_side_fs_request_is_refused(env):
    hub, _ = env
    await hub.push(deliver("t1", "FS"))
    assert "fs answer: -32601" in finals(await hub.collect_turn("t1"))[-1]["text"]


async def test_stop_by_trusted_human_cancels_the_turn(env):
    hub, _ = env
    await hub.push(deliver("t1", "SLOW"))
    await hub.next(lambda f: f["type"] == "message.final")
    await hub.push({"type": "turn.stop", "thread": "t1", "by": OTHER})  # not trusted: ignored
    with pytest.raises(TimeoutError):
        await hub.next(lambda f: f["type"] == "turn.ended", timeout=0.7)
    await hub.push({"type": "turn.stop", "thread": "t1", "by": TRUSTED})
    end = await hub.next(lambda f: f["type"] == "turn.ended", timeout=5)
    assert end["stopped"] is True and end["acted"] is False


async def test_agent_death_is_reported_then_the_agent_restarts(env):
    hub, _ = env
    await hub.push(deliver("t1", "hello", mid="a"))
    await hub.collect_turn("t1")
    await hub.push(deliver("t1", "DIE", mid="b"))
    text = finals(await hub.collect_turn("t1"))[-1]["text"]
    assert text.startswith("about to die") and "the agent could not run this turn" in text
    assert "exited (code 3)" in text
    await hub.push(deliver("t1", "back", mid="c"))
    # new process: the old session is unknown to it (session/load refused), so a new one starts
    assert finals(await hub.collect_turn("t1"))[-1]["text"] == "Echo[s1#1]: back"


async def test_no_text_with_unusual_stop_reason_says_so(env):
    hub, _ = env
    await hub.push(deliver("t1", "SILENT"))
    assert finals(await hub.collect_turn("t1"))[-1]["text"] == "(the agent ended its turn without a reply: refusal)"


def test_reject_option():
    assert reject_option([{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_always"}]) == \
        {"outcome": {"outcome": "selected", "optionId": "r"}}
    assert reject_option([{"optionId": "a", "kind": "allow_once"}]) == {"outcome": {"outcome": "cancelled"}}
    assert reject_option(None) == {"outcome": {"outcome": "cancelled"}}


def test_prompt_text_frames_context_as_data():
    f = deliver("t1", "do it")
    f["context"]["unseen"] = [{"who": "Bob", "text": "line1\nline2"}]
    f["message"]["files"] = [{"id": "f"}]
    t = prompt_text(f)
    assert t.startswith('[briefling thread "Port Kiro", message from Ada]')
    assert "> Bob: line1\n> line2" in t and "1 attachment(s) not forwarded" in t and t.endswith("\n\ndo it")


def test_run_exits_when_the_agent_cannot_start(tmp_path, caplog):
    conf = {"BRIEFLING_WS_URL": "ws://127.0.0.1:9/ws", "BRIEFLING_TOKEN_URL": "http://127.0.0.1:9/t",
            "BRIEFLING_CLIENT_ID": "c", "BRIEFLING_CLIENT_SECRET": "s", "BRIEFLING_ALLOWED_USERS": TRUSTED}
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('error: You are not logged in\\n'); sys.exit(1)"]
    with pytest.raises(SystemExit):
        asyncio.run(Bridge(conf, cmd, str(tmp_path)).run())
    assert "the agent command failed to start" in caplog.text and "You are not logged in" in caplog.text


def test_run_refuses_trust_all_tools(tmp_path, capsys):
    assert cli.main(["run", "--env", str(tmp_path / "e"), "--", "kiro-cli", "acp", "--trust-all-tools"]) == 2
    assert "refusing --trust-all-tools" in capsys.readouterr().err


def test_join_writes_private_env_without_printing_the_secret(tmp_path, capsys, monkeypatch):
    joined = {"agent": "kiro", "spaces": ["sp"], "allowed": [TRUSTED],
              "credentials": {"client_id": "cid", "client_secret": "s3cr3t-xyz", "token_url": "https://t/oauth2/token"}}

    async def redeem(app, code, timeout):
        assert app == "https://b.example" and len(code) == 32
        return "wss://hub.example", joined
    monkeypatch.setattr(cli.join, "redeem", redeem)
    path = tmp_path / "conf" / "bridge.env"
    assert cli.main(["join", f"https://b.example/agent.md?code={'A' * 32}", "--env", str(path)]) == 0
    out = capsys.readouterr()
    assert "s3cr3t" not in out.out + out.err
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    env = cli.read_env(str(path))
    assert env["BRIEFLING_CLIENT_SECRET"] == "s3cr3t-xyz" and env["BRIEFLING_ALLOWED_USERS"] == TRUSTED
    assert env["BRIEFLING_WS_URL"] == "wss://hub.example"
    assert cli.main(["join", f"https://b.example/agent.md?code={'A' * 32}", "--env", str(path)]) == 1  # no overwrite
