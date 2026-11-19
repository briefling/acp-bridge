"""Behind a corporate proxy: the machine's DNS knows only internal names, so the hub is reachable only through the
proxy. The bridge's token request and hub WebSocket go through it, loopback never does, NO_PROXY is honoured."""

import asyncio
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from briefling_acp import join  # noqa: E402
from briefling_acp.bridge import Bridge  # noqa: E402
from test_bridge import FAKE, TRUSTED, Hub, deliver, finals  # noqa: E402

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "WSS_PROXY", "WS_PROXY")


@pytest.fixture
def clean_env(monkeypatch):
    for v in PROXY_VARS:
        monkeypatch.delenv(v, raising=False)
        monkeypatch.delenv(v.lower(), raising=False)
    # No system proxy settings either (macOS/Windows): only the variables of each test count.
    monkeypatch.setattr(join.urllib.request, "getproxies", join.urllib.request.getproxies_environment)
    monkeypatch.setattr(join.urllib.request, "proxy_bypass",
                        lambda h: join.urllib.request.proxy_bypass_environment(h))
    return monkeypatch


def test_proxy_for_follows_the_scheme_no_proxy_and_never_loopback(clean_env):
    assert join.proxy_for("https://briefling.example/config.json") is None, "no proxy configured: direct"
    clean_env.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    clean_env.setenv("HTTP_PROXY", "http://plain.corp:8080")
    assert join.proxy_for("https://briefling.example/config.json") == "http://proxy.corp:3128"
    assert join.proxy_for("wss://ws.briefling.example/prod") == "http://proxy.corp:3128", "wss = the https proxy"
    assert join.proxy_for("ws://hub.example/ws") == "http://plain.corp:8080", "ws = the http proxy"
    for local in ("http://127.0.0.1:8787/oauth2/token", "ws://localhost:8787/ws", "http://[::1]:1/x", "http://a.localhost/"):
        assert join.proxy_for(local) is None, local
    clean_env.setenv("NO_PROXY", ".internal.corp,briefling.example")
    assert join.proxy_for("https://briefling.example/x") is None
    assert join.proxy_for("https://git.internal.corp/x") is None
    assert join.proxy_for("wss://other.example/ws") == "http://proxy.corp:3128"
    clean_env.delenv("HTTPS_PROXY")
    clean_env.delenv("HTTP_PROXY")
    clean_env.setenv("ALL_PROXY", "http://all.corp:1")
    assert join.proxy_for("wss://other.example/ws") == "http://all.corp:1"


def test_proxy_for_windows_settings_without_scheme_and_broken_settings(clean_env):
    clean_env.setattr(join.urllib.request, "getproxies", lambda: {"https": "proxy.corp:8080"})
    clean_env.setattr(join.urllib.request, "proxy_bypass", lambda h: h.endswith(".corp"))
    assert join.proxy_for("wss://hub.example/ws") == "http://proxy.corp:8080"
    assert join.proxy_for("https://intranet.corp/") is None, "the settings' bypass list"

    def boom():
        raise OSError("registry")
    clean_env.setattr(join.urllib.request, "getproxies", boom)
    assert join.proxy_for("wss://hub.example/ws") is None, "a broken setting falls back to direct"


async def start_proxy(port_of):
    """A forward proxy (CONNECT and absolute-URI requests, then a raw pipe: WebSocket upgrades pass) that alone can
    resolve the names of `port_of`, like a corporate proxy in front of a DNS that knows only internal names."""
    seen = []

    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            w.close()

    async def handle(cr, cw):
        head = await cr.readuntil(b"\r\n\r\n")
        method, target, rest = head.split(b" ", 2)
        if method == b"CONNECT":
            host = target.decode().split(":")[0]
            seen.append(("CONNECT", host))
            ur, uw = await asyncio.open_connection("127.0.0.1", port_of[host])
            cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await cw.drain()
        else:
            u = target.decode()
            host = u.split("://", 1)[1].split("/", 1)[0].split(":")[0]
            seen.append((method.decode(), u))
            ur, uw = await asyncio.open_connection("127.0.0.1", port_of[host])
            uw.write(method + b" /" + u.split("://", 1)[1].split("/", 1)[1].encode() + b" " + rest)
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1], seen


async def test_bridge_reaches_a_hub_known_only_to_the_proxy(clean_env, tmp_path):
    hub = Hub()
    app = web.Application()
    app.router.add_post("/token", hub.token)
    app.router.add_get("/ws", hub.wsh)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    proxy, pport, seen = await start_proxy({"hub.invalid": port})
    clean_env.setenv("HTTP_PROXY", f"http://127.0.0.1:{pport}")
    conf = {"BRIEFLING_WS_URL": f"ws://hub.invalid:{port}/ws", "BRIEFLING_TOKEN_URL": f"http://hub.invalid:{port}/token",
            "BRIEFLING_CLIENT_ID": "cid", "BRIEFLING_CLIENT_SECRET": "secret", "BRIEFLING_ALLOWED_USERS": TRUSTED}
    bridge = Bridge(conf, FAKE, str(tmp_path), state_path=str(tmp_path / "s.json"))
    task = asyncio.create_task(bridge.run())
    try:
        assert (await hub.next())["type"] == "subscribe", "connected through the proxy (.invalid never resolves)"
        # The token request opened the proxy connection; the WebSocket reused it (keep-alive, piped raw): both
        # reached hub.invalid, a name only the proxy resolves.
        assert seen[0] == ("POST", f"http://hub.invalid:{port}/token")
        await hub.push(deliver("t1", "hello"))
        assert finals(await hub.collect_turn("t1"))[-1]["text"].startswith("Echo[")
    finally:
        bridge.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        proxy.close()
        await runner.cleanup()
