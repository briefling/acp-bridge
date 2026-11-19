"""Self-update of the bridge (selfupdate.py): the gates with a scripted `run`, then the bridge's own wiring against the
fake hub (report at subscribe, release frame, wait for no turn, restart)."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from briefling_acp import __main__ as cli  # noqa: E402
from briefling_acp import __version__, selfupdate  # noqa: E402
from test_bridge import deliver, env, running  # noqa: E402,F401  (env: the fixture)

OLD, NEW = "a" * 40, "b" * 40
REL = {"version": "9.0.0", "commit": NEW}


def git_install(installer="pip", commit=OLD, **kw):
    return selfupdate.Install(installer, commit, selfupdate.REPO_URL, False, "/venv", **kw)


class Script:
    """A fake `run`: answers by the command's shape, records every call."""

    def __init__(self, tag=NEW, install_ok=True, after=None):
        self.calls, self.tag, self.install_ok = [], tag, install_ok
        self.now = {"version": "0.0.1", "commit": OLD}
        self.after = after or {"version": "9.0.0", "commit": NEW}

    def __call__(self, argv, timeout=0):
        self.calls.append(list(argv))
        done = lambda out="", rc=0: subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")  # noqa: E731
        if argv[:2] == ["git", "ls-remote"]:
            return done(f"{self.tag}\t{argv[3]}\n" if self.tag else "")
        if "install" in argv:
            target = argv[-1].rsplit("@", 1)[1]
            if target != OLD and not self.install_ok:
                return done(rc=1)
            self.now = self.after if target != OLD else {"version": "0.0.1", "commit": OLD}
            return done()
        if argv[1] == "-c":
            return done(json.dumps(self.now))
        raise AssertionError(argv)

    def installs(self):
        return [c[-1].rsplit("@", 1)[1] for c in self.calls if "install" in c]


def test_installs_the_announced_commit_once_the_public_tag_agrees(tmp_path):
    run = Script()
    assert selfupdate.apply(git_install(), REL, "0.0.1", str(tmp_path / "u.json"), run=run) is True
    assert run.installs() == [NEW]
    pip = next(c for c in run.calls if "install" in c)
    assert pip[:4] == [sys.executable, "-m", "pip", "install"] and pip[-1] == f"git+{selfupdate.REPO_URL}@{NEW}"


def test_a_tag_that_disagrees_installs_nothing_and_records_nothing(tmp_path):
    for tag in ("c" * 40, ""):
        run = Script(tag=tag)
        assert selfupdate.apply(git_install(), REL, "0.0.1", str(tmp_path / "u.json"), run=run) is False
        assert run.installs() == [] and not (tmp_path / "u.json").exists()


def test_never_a_downgrade_nor_the_same_version(tmp_path):
    run = Script()
    for current in ("9.0.0", "9.0.1"):
        assert selfupdate.apply(git_install(), REL, current, str(tmp_path / "u.json"), run=run) is False
    assert run.calls == []


def test_failed_install_puts_the_previous_commit_back_and_is_never_retried(tmp_path):
    state = str(tmp_path / "u.json")
    run = Script(install_ok=False)
    assert selfupdate.apply(git_install(), REL, "0.0.1", state, run=run) is False
    assert run.installs() == [NEW, OLD], "rolled back to the commit it ran"
    assert selfupdate.read_state(state) == {"version": "9.0.0", "commit": NEW}
    if os.name != "nt":  # no mode bits on Windows (the profile's ACL)
        assert oct((tmp_path / "u.json").stat().st_mode & 0o777) == "0o600"
    again = Script()
    assert selfupdate.apply(git_install(), REL, "0.0.1", state, run=again) is False and again.installs() == []
    assert selfupdate.blocked(git_install(), True, "0.0.1", selfupdate.read_state(state)) == "failed"
    newer = {"version": "9.0.1", "commit": "d" * 40}
    assert selfupdate.apply(git_install(), newer, "0.0.1", state, run=Script(tag="d" * 40, after={"version": "9.0.1", "commit": "d" * 40})) is True


def test_an_install_that_does_not_report_the_release_is_rolled_back(tmp_path):
    run = Script(after={"version": "8.0.0", "commit": NEW})  # wrong version inside the commit
    assert selfupdate.apply(git_install(), REL, "0.0.1", str(tmp_path / "u.json"), run=run) is False
    assert run.installs() == [NEW, OLD]


def test_uv_installs_into_this_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.shutil, "which", lambda n: "/bin/uv")
    run = Script()
    assert selfupdate.apply(git_install("uv"), REL, "0.0.1", str(tmp_path / "u.json"), run=run) is True
    uv = next(c for c in run.calls if "install" in c)
    assert uv[:5] == ["uv", "pip", "install", "-q", "--python"] and uv[5] == sys.executable


def test_blocked_reasons(monkeypatch):
    monkeypatch.setattr(selfupdate.shutil, "which", lambda n: None)
    assert selfupdate.blocked(git_install(), False, "0.0.1", {}) == "off"
    assert selfupdate.blocked(git_install("uv", uv_cache="/c/uv"), True, "0.0.1", {}) == "manual", "uv missing"
    eph = selfupdate.Install("uv", OLD, selfupdate.REPO_URL, False, "/c/uv/archive-v0/x", "/c/uv")
    assert selfupdate.blocked(eph, True, "0.0.1", {}) == "pinned", "uvx: the launcher picks the version"
    assert selfupdate.blocked(selfupdate.Install("pip", "", "file:///src/acp-bridge", True, "/v"), True, "0.0.1", {}) == "manual"
    assert selfupdate.blocked(selfupdate.Install("pip", OLD, "https://github.com/someone/fork", False, "/v"), True, "0.0.1", {}) == "manual"
    assert selfupdate.blocked(git_install(), True, "0.0.1", {}) is None
    assert selfupdate.blocked(git_install(), True, "0.0.1", {"version": "0.0.1"}) is None, "a failure older than this run"


def test_valid_release_rejects_malformed_values():
    assert selfupdate.valid_release(REL) == REL
    for bad in (None, {}, {"version": "9.0", "commit": NEW}, {"version": "9.0.0", "commit": "abc"}, "x"):
        assert selfupdate.valid_release(bad) is None


def test_this_install_is_read_from_its_own_metadata():
    i = selfupdate.Install.here()  # the test checkout: not a git install of the public repo
    assert selfupdate.blocked(i, True, __version__, {}) in ("manual", None)


# -- the bridge ---------------------------------------------------------------------------


async def test_subscribe_reports_the_version_and_why_it_will_not_update(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.Install, "here", classmethod(lambda c: selfupdate.Install("", "", "", True, "/v")))
    monkeypatch.setenv(selfupdate.FROM_ENV, "0.0.1")
    async with running(tmp_path, update_path=str(tmp_path / "u.json")) as (hub, _):
        assert hub.subscribe["bridge"] == {"version": __version__, "update": "manual", "updated_from": "0.0.1"}


async def test_no_update_path_means_no_self_update(env):
    hub, bridge = env
    assert hub.subscribe["bridge"] == {"version": __version__} and not bridge.auto_update


async def test_announced_release_installs_then_restarts_once_no_turn_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.Install, "here", classmethod(lambda c: git_install()))
    calls = []
    monkeypatch.setattr(selfupdate, "apply", lambda inst, rel, cur, path: calls.append(rel) or True)
    monkeypatch.setattr("briefling_acp.bridge.UPDATE_IDLE_POLL_S", 0.05)
    async with running(tmp_path, update_path=str(tmp_path / "u.json")) as (hub, bridge):
        await hub.push(deliver("t1", "SLOW"))
        await hub.next(lambda f: f["type"] == "message.final")
        await hub.push({"type": "release", "release": REL})
        await asyncio.sleep(0.4)
        assert calls == [REL] and not bridge.restart, "waits for the running turn"
        await hub.push({"type": "turn.stop", "thread": "t1", "by": "sub-trusted"})
        await hub.next(lambda f: f["type"] == "turn.ended")
        async with asyncio.timeout(5):
            await hub.task
        assert bridge.restart and not bridge.agent.alive


async def test_old_or_malformed_release_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.Install, "here", classmethod(lambda c: git_install()))
    monkeypatch.setattr(selfupdate, "apply", lambda *a: pytest.fail("must not install"))
    async with running(tmp_path, update_path=str(tmp_path / "u.json")) as (hub, bridge):
        for rel in ({"version": __version__, "commit": NEW}, {"version": "9.0.0"}, "x"):
            await hub.push({"type": "subscribed", "spaces": [], "release": rel})
        await asyncio.sleep(0.2)
        assert bridge.update_task is None


async def test_off_in_the_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.Install, "here", classmethod(lambda c: git_install()))
    async with running(tmp_path, update_path=str(tmp_path / "u.json")) as (hub, bridge):
        pass
    from briefling_acp.bridge import Bridge
    b = Bridge({**{k: "x" for k in cli.KEYS[:4]}, "BRIEFLING_ALLOWED_USERS": "a", selfupdate.OFF_ENV: "0"}, ["true"],
               str(tmp_path), update_path=str(tmp_path / "u.json"))
    assert not b.auto_update and "BRIEFLING_ACP_AUTO_UPDATE" in cli.KEYS


def test_restart_keeps_the_arguments_but_not_the_link(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "do_join", lambda *a: 0)
    monkeypatch.setattr(cli, "do_run", lambda env, command, cwd, port, host, restart_args: seen.update(r=restart_args) or 0)
    cli.main(["https://b.example/#/join/x", "--env", "e", "--mcp-port", "8765", "--", "claude-agent-acp"])
    assert seen["r"] == ["--env", "e", "--mcp-port", "8765", "--", "claude-agent-acp"]
    assert selfupdate.restart_argv(seen["r"])[:3] == [sys.executable, "-m", "briefling_acp"]
    cli.main(["--env", "e"])
    assert seen["r"] == ["--env", "e"]
