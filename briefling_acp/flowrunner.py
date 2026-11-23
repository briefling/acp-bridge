"""Flow runner: the hosts of one agent process (``FlowHost`` per space), their timer and the hub's flow frames, with
no tie to the Hermes gateway. docs/design-flow.md § 4 (tick, task, done), § 13-15 (pilot, switch, piloted by default).

What it needs from the process that runs the agent is the small ``FlowGateway`` port: send a frame, make a request,
start a task turn (an internal event in the issue thread), and say whether a turn runs in a thread. The process tells
it in return when a turn ended (``turn_ended``: the task / discussion spend, then an immediate tick). The Hermes plugin
(``adapter.py``) is one such process; the ACP bridge is meant to be the next one, sharing this file by symlink.
Stdlib only, like ``flow/``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import flowswitch
from .flowhost import FlowHost, FlowSpace, load_settings
from .flowswitch import SwitchError, parse_link
from .tooldefs import ID_RE

logger = logging.getLogger(__name__)

FLOW_TIMER_S = 30            # how often the timer checks whether a host is due (poll_min)
FLOW_WAKE_MIN_S = 60         # flow.wake: ignored less than this after the host's last tick (docs/design-flow.md § 4.4)


@dataclass
class FlowGateway:
    """What the runner asks of the agent's process.

    send(frame) -> bool: a frame to the hub, False when not sent (not connected).
    request(frame) -> reply: a frame answered by the hub; raises ``flowhost.HubAnswer(code)`` on a hub refusal.
    inject(space, thread, title, text): start the task turn in the issue thread (no human sent it).
    turn_running(thread): a turn of this agent runs there now. turn_since(thread): its start, in seconds (0 = none).
    connected: set while the hub is reachable (the timer waits for it, flow_switch needs it).
    running(): the process still runs (False after its disconnect: the timer stops).
    """
    send: Callable[[Dict[str, Any]], Awaitable[bool]]
    request: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
    inject: Callable[[str, str, str, str], Awaitable[None]]
    turn_running: Callable[[str], bool]
    turn_since: Callable[[str], int]
    connected: asyncio.Event
    running: Callable[[], bool] = lambda: True


class FlowRunner:
    """The flow of one process: hosts per space (settings file, or made on the hub's `flow.pilot true`), the timer,
    the hub's flow frames, the turn ends. `name` only labels the logs."""

    def __init__(self, gw: FlowGateway, settings: str, auto: bool, state_dir: Callable[[], Path], name: str = "briefling"):
        self.gw, self.name, self._state_dir = gw, name, state_dir
        self.poll_min, self.spaces = load_settings(settings)
        self.auto = auto                                  # § 15: hosts made on `flow.pilot true` with no settings entry
        self.hosts: Dict[str, FlowHost] = {}              # space -> host
        self.turns: Dict[str, FlowHost] = {}              # thread -> host whose task turn runs there
        self.ended: Dict[str, int] = {}                   # thread -> end (s) of its last turn: the task's sign of life
        self.cfg_wait: Dict[str, Dict[str, Any]] = {}     # space -> last flow.config received before its host
        self.config_lock = asyncio.Lock()                 # one bundle written at a time
        self.loop_task: Optional[asyncio.Task] = None

    # -- life ------------------------------------------------------------------
    def start(self) -> None:
        """The settings file's hosts and the timer (nothing to do without entries: hosts come on `flow.pilot`)."""
        if not self.spaces:
            return
        state_dir = self._state_dir()
        for fs in self.spaces:
            self.hosts[fs.space] = FlowHost(fs, state_dir, self.gw.request, self.inject_for(fs.space))
        logger.info("[%s] flow on for %s (every %.0f min)", self.name, ",".join(self.hosts), self.poll_min)
        self.loop_task = asyncio.create_task(self.loop())

    def stop(self) -> None:
        if self.loop_task:
            self.loop_task.cancel()
            self.loop_task = None

    # -- subscribe -------------------------------------------------------------
    def no_uns(self) -> List[str]:
        """This machine's flow agents with no unstrikeable state file (`poll-<agent>.json` where `uns_state` or the
        default place says): never ran on unstrikeable here, so briefling pilots them without a link (§ 14). An
        unreadable path counts as present: the link stays the way in."""
        out = set()
        for fs in self.spaces or []:
            try:
                if not flowswitch.uns_state_path(fs.agent, fs.uns_state).exists():
                    out.add(fs.agent)
            except (SwitchError, OSError, ValueError):
                continue
        return sorted(out)[:20]

    def uns(self) -> List[str]:
        """Agents with an unstrikeable state file on this machine (§ 15): the `poll-<agent>.json` of the default place
        (`$UNS_HOME/state/`, else `~/.unstrikeable/state/`), plus the settings file's agents not in `no_uns`. The hub
        pilots by default the instance's other agents; these keep the switch by link."""
        out = {fs.agent for fs in self.spaces or []} - set(self.no_uns())
        try:
            base = flowswitch.uns_state_path("x", None).parent
            for p in base.glob("poll-*.json") if base.is_dir() else []:
                name = p.name[len("poll-"):-len(".json")]
                if flowswitch.AGENT_RE.match(name):
                    out.add(name)
        except OSError:
            pass
        return sorted(out)[:50]

    def unpilot_all(self) -> None:
        """A new connection: nobody pilots until the hub's flow.pilot says so."""
        for host in list(self.hosts.values()):
            host.piloted = False

    async def report_all(self) -> None:
        """The hub may hold an old state: report it again (piloted hosts only)."""
        for host in list(self.hosts.values()):
            await self.report(host, force=True)

    # -- hosts and timer -------------------------------------------------------
    def host_of_space(self, space: str) -> Optional[FlowHost]:
        return self.hosts.get(space)

    async def auto_host(self, space: str, agent: str) -> Optional[FlowHost]:
        """§ 15: the hub pilots `agent` in `space` by default and this machine has no settings entry there: a host
        with no local config nor identity (config = the hub's `flow.config`, GitLab board = the hub, GitHub = `gh`)."""
        if not self.auto or not (ID_RE.match(space) and flowswitch.AGENT_RE.match(agent)):
            return None
        host = FlowHost(FlowSpace(space=space, agent=agent), self._state_dir(), self.gw.request, self.inject_for(space))
        self.hosts[space] = host
        logger.info("[%s] flow on for %s/%s, piloted by briefling by default (no BRIEFLING_FLOW entry)", self.name, space, agent)
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self.loop())
        waiting = self.cfg_wait.pop(space, None)
        if waiting is not None:   # the hub sends the config before flow.pilot at a connection
            await self.on_config(waiting)
        return host

    async def loop(self) -> None:
        """Timer: each host ticks every `poll_min` while the hub is reachable (degraded mode is a later slice)."""
        while self.gw.running():
            try:
                await self.gw.connected.wait()
                for host in list(self.hosts.values()):
                    # the config's limits.poll_min wins over the settings file (floor 1 min)
                    if getattr(host, "piloted", False) and time.time() - host.last_tick >= max(1.0, host.poll_min or self.poll_min) * 60:
                        await self.tick(host)
                await asyncio.sleep(FLOW_TIMER_S)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning("[%s] flow timer error", self.name, exc_info=True)
                await asyncio.sleep(FLOW_TIMER_S)

    async def tick(self, host: FlowHost) -> str:
        if not getattr(host, "piloted", False):   # unstrikeable (or nobody) pilots this agent: no tick, no report
            return "not piloted"
        thread = host.thread
        alive = int(time.time()) if thread and self.gw.turn_running(thread) else self.ended.get(thread or "")
        try:
            what = await host.tick(alive)
        except Exception:
            logger.warning("[%s] flow tick %s failed", self.name, host.fs.space, exc_info=True)
            return "error"
        logger.info("[%s] flow tick %s/%s: %s", self.name, host.fs.space, host.fs.agent, what)
        await self.report(host)
        return what

    async def report(self, host: FlowHost, force: bool = False) -> None:
        """`flow.state` to the hub when what it shows changed (pause, task, spend, caps, alerts), or `force`."""
        snap = getattr(host, "snapshot", None)
        if snap is None or not getattr(host, "piloted", False):
            return
        thread = host.thread
        in_turn = bool(thread and self.gw.turn_running(thread))
        running = bool(thread and (getattr(host, "task_turn", False) or in_turn))
        alive = self.gw.turn_since(thread or "") if running and in_turn else self.ended.get(thread or "")
        state = snap(running=running, alive=alive or None)
        if not force and state == host.reported:
            return
        if await self.gw.send({"type": "flow.state", "space": host.fs.space, "agent": host.fs.agent, "state": state}):
            host.reported = state

    # -- turns -----------------------------------------------------------------
    def inject_for(self, space: str):
        """The host's `inject(thread, title, text)`: the task turn through the gateway, remembered as this host's."""
        async def inject(thread: str, title: str, text: str) -> None:
            self.turns[thread] = self.hosts[space]
            await self.gw.inject(space, thread, title, text)
        return inject

    def turn_ended(self, thread: str, space: Optional[str], task: bool, usage: Optional[Dict[str, Any]]) -> None:
        """A turn of this agent ended in `thread` (of `space`, when known). Spend tagged `task` (the flow's turn) or
        `discussion` (a human in a flow space); a task turn's end ticks at once, so a closed task is seen without
        waiting for the timer. `usage` = None when the hub did not take it."""
        self.ended[thread] = int(time.time())
        host = self.turns.get(thread) if task else None
        if host is not None:
            self.turns.pop(thread, None)
            host.add_usage("task", usage)
            host.turn_ended()
            asyncio.ensure_future(self.tick(host))
            return
        host = self.hosts.get(space or "")
        if host is not None:
            host.add_usage("discussion", usage)
            asyncio.ensure_future(self.report(host))

    # -- hub frames ------------------------------------------------------------
    async def on_pause(self, frame: Dict[str, Any]) -> None:
        """Pause / Resume of this machine's agent from briefling (a trusted human; the hub checked it)."""
        host = self.hosts.get(str(frame.get("space") or ""))
        paused = frame.get("paused")
        if host is None or frame.get("agent") != host.fs.agent or not isinstance(paused, bool):
            return
        by = re.sub(r"[\x00-\x1f\x7f]+", " ", str(frame.get("by") or "a human"))[:80]
        changed = await host.set_paused(paused, by)
        logger.info("[%s] flow %s/%s %s by %s%s", self.name, host.fs.space, host.fs.agent, "paused" if paused else "resumed",
                    by, "" if changed else " (no change)")
        await self.report(host, force=True)
        if changed and not paused:
            self.on_wake({"space": host.fs.space})

    def on_wake(self, frame: Dict[str, Any]) -> None:
        """« Read the board now »: a normal tick, unless the host ticked less than FLOW_WAKE_MIN_S ago."""
        host = self.hosts.get(str(frame.get("space") or ""))
        if host is None or not getattr(host, "piloted", False):
            return
        if time.time() - host.last_tick < FLOW_WAKE_MIN_S:
            logger.debug("[%s] flow wake %s skipped: ticked %.0f s ago", self.name, host.fs.space, time.time() - host.last_tick)
            return
        asyncio.ensure_future(self.tick(host))

    async def on_pilot(self, frame: Dict[str, Any]) -> None:
        """Who pilots this machine's agent in the space (§ 13): true = tick, report, ask tasks; false = stop, and
        after a switch write the engine state back to unstrikeable's file (« Back to unstrikeable »)."""
        host = self.hosts.get(str(frame.get("space") or ""))
        piloted = frame.get("piloted")
        if host is None and piloted is True and isinstance(frame.get("space"), str) and isinstance(frame.get("agent"), str):
            host = await self.auto_host(frame["space"], frame["agent"])
        if host is None or frame.get("agent") != host.fs.agent or not isinstance(piloted, bool):
            return
        was, host.piloted = host.piloted, piloted
        if piloted != was:
            logger.info("[%s] flow %s/%s %s", self.name, host.fs.space, host.fs.agent,
                        "piloted by briefling" if piloted else "not piloted by briefling")
        if piloted:
            await self.report(host, force=True)
            self.on_wake({"space": host.fs.space})
        elif isinstance(host.state.get("switched"), dict):
            await host.switch_back()

    async def on_config(self, frame: Dict[str, Any]) -> None:
        """The space's flow config from the hub's editor: loaded by the host of that space (its agent), answered with
        `flow.config.status`. A space this machine runs no flow for is ignored: the editor then shows no answer."""
        host = self.hosts.get(str(frame.get("space") or ""))
        version, sha = frame.get("version"), frame.get("sha")
        if not isinstance(version, int) or isinstance(version, bool) or not isinstance(sha, str):
            return
        if host is None:   # no host yet: kept for the one `flow.pilot true` may make (§ 15), the latest only
            space = frame.get("space")
            if self.auto and isinstance(space, str) and ID_RE.match(space):
                self.cfg_wait = {**{k: v for k, v in self.cfg_wait.items() if k != space}, space: frame}
                while len(self.cfg_wait) > 20:
                    self.cfg_wait.pop(next(iter(self.cfg_wait)))
            return
        async with self.config_lock:
            try:
                ok, error = await asyncio.to_thread(host.apply_config, version, sha, frame.get("files"))
            except Exception as e:  # disk full, permissions: reported, the previous bundle stays
                ok, error = False, "%s: %s" % (type(e).__name__, str(e)[:200])
        logger.info("[%s] flow config %s v%s for %s: %s", self.name, host.fs.space, version, host.fs.agent, "ok" if ok else error)
        status = {"type": "flow.config.status", "space": host.fs.space, "agent": host.fs.agent, "version": version, "sha": sha, "ok": ok}
        if not ok:
            status["error"] = error or "error"
        commit = frame.get("commit")
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):   # git source: the head it came from
            status["commit"] = commit
        await self.gw.send(status)

    # -- switch by link (§ 13) -------------------------------------------------
    async def switch(self, link: str) -> Dict[str, Any]:
        """`flow_switch`: redeem a switch link for the host of (space, agent), on the runner's loop."""
        try:
            code, space, agent = parse_link(link)
        except SwitchError as e:
            return {"error": str(e)}
        host = self.hosts.get(space)
        if host is None or host.fs.agent != agent:
            return {"error": "this gateway runs no flow for %s in %s: add a BRIEFLING_FLOW entry for (space %s, agent %s) "
                             "with its identity and uns_state, then restart the gateway" % (agent, space, space, agent)}
        if not self.gw.connected.is_set():
            return {"error": "briefling hub not connected: retry when connected"}
        try:
            out = await host.switch(code)
        except SwitchError as e:
            return {"error": str(e)}
        await self.report(host, force=True)
        asyncio.ensure_future(self.tick(host))
        return out
