"""Flow host: runs the board engine (``briefling.flow``) for this plugin's agent in board-driven spaces.

docs/design-flow.md § 4.3: a tick (timer every ``poll_min``, or right after a task turn) reads the board; an event
becomes a task the hub places in the issue thread (``flow.task``), the turn runs in that thread's session, and the
closed task goes back to the hub (``flow.done``). The board says what to do, the hub says where and whether now.

Settings: a JSON file (``BRIEFLING_FLOW`` / extra ``flow``), read at start::

    {"poll_min": 5, "spaces": [{"space": "sandbox", "agent": "kevin", "config": "/path/to/hq-clone",
      "identity": {"app_id": 123, "app_key": "/path/key.pem", "installation_id": 456},
      "uns_state": "~/.unstrikeable/state/poll-kevin.json"}]}

The company config bundle (``config.yml``, ``culture.md``, ``agents/``, ``flows/``, ``playbooks/``) comes from the hub's
flow editor (``flow.config``, see ``flowconfig.py``): the last good one it pushed is in use. ``config`` = an optional
local folder used until the hub has pushed one (spaces without a config in briefling). ``identity`` = the agent's GitHub
App (its key stays on this machine, never in the bundle); missing = this machine's ``gh`` login. ``uns_state`` =
unstrikeable's ``poll-<agent>.json`` as this gateway sees it, for « switch by link » (``flowswitch.py``); missing =
``$UNS_HOME/state/`` else ``~/.unstrikeable/state/``.

Pilot (docs/design-flow.md § 13): the host ticks, reports and asks tasks only while the hub's last ``flow.pilot`` said
true (``piloted``, memory only, false at each new connection until the hub says otherwise).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .flow.config import Company, ConfigError, Department, load_company
from .flowconfig import ConfigStore
from . import flowswitch
from .flowswitch import SwitchError
from .flow.backends.github import GitHubBoard
from .flow.backends.gitlab import GitLabBoard, GitLabError
from .flow.identity import make_token
from .flow.model import AGENT_MARK
from .flow.poll import MONTH_S, poll
from .flow.usage import USAGE_KEYS, usage_delta
from .flow.status import parse_status, status_body

logger = logging.getLogger(__name__)

DEFAULT_POLL_MIN = 5.0
MIN_POLL_MIN = 1.0
NOTHING_LEARNED = {"none", "nothing", "-"}
ZERO = {k: 0 for k in USAGE_KEYS}
ALERTS_SHOWN = 5                 # flow.state: the latest engine alerts of the last week (lost task, quota pause)
ALERTS_AGE_S = 7 * 86400
TAGS = ("task", "discussion")
HUB_CALL_S = 30.0               # a board read through the hub (GitLab): one page of issues and their notes


def local_day(ts: float) -> str:
    """Calendar day in local time, as the engine's daily quota counts it."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class HubAnswer(Exception):
    """The hub refused a frame (its error code): retrying the same frame cannot help."""


class FlowError(Exception):
    """Refusal shown to the agent as the tool's error (bad ref, unknown state, assignment label...)."""


@dataclass
class FlowSpace:
    space: str
    agent: str
    config: Optional[Path] = None     # local fallback until the hub pushes a bundle
    identity: Dict[str, Any] = field(default_factory=dict)
    uns_state: Optional[Path] = None  # unstrikeable's poll-<agent>.json (switch by link); None = default place


def load_settings(path: str) -> tuple[float, List[FlowSpace]]:
    """(poll_min, spaces) from the settings file; an unreadable or malformed file = no flow (logged)."""
    if not path:
        return DEFAULT_POLL_MIN, []
    try:
        raw = json.loads(Path(os.path.expanduser(path)).read_text())
        poll_min = max(MIN_POLL_MIN, float(raw.get("poll_min") or DEFAULT_POLL_MIN))
        spaces = [FlowSpace(space=str(s["space"]), agent=str(s["agent"]),
                            config=Path(os.path.expanduser(str(s["config"]))) if s.get("config") else None,
                            identity=dict(s.get("identity") or {}),
                            uns_state=Path(os.path.expanduser(str(s["uns_state"]))) if s.get("uns_state") else None)
                  for s in raw.get("spaces") or []]
        for fs in spaces:   # kept (the flow still runs), refused at switch time with the same message
            if fs.uns_state is not None and fs.uns_state.name != flowswitch.state_name(fs.agent):
                logger.warning("briefling flow settings: uns_state of %s/%s must be named %s", fs.space, fs.agent,
                               flowswitch.state_name(fs.agent))
        return poll_min, spaces
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning("briefling flow settings %s unreadable: %s", path, type(e).__name__)
        return DEFAULT_POLL_MIN, []


def bots(co: Company) -> set:
    """Logins trusted as machines: agent identities and `trusted_bots` (their words are never a human go)."""
    return {str(a.get("identity", "")).lower() for a in co.agents.values() if a.get("identity")} | set(co.trusted_bots)


def department_of(co: Company, agent: str, ref: str) -> Department:
    """The agent's department owning the repo of `ref` (`owner/repo#N`): the board tools act nowhere else."""
    repo, sep, num = ref.partition("#")
    if not sep or not num.isdigit():
        raise FlowError("ref must look like owner/repo#123")
    found = [d for d in co.departments_of(agent) if repo.lower() in (r.lower() for r in d.repos)]
    if len(found) != 1:
        raise FlowError("%s is not in one of your departments' repos" % repo if not found
                        else "%s belongs to several departments" % repo)
    return found[0]


def to_counters(u: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """The plugin's per-turn usage (hub format) in the engine's counter keys."""
    u = u or {}
    return {"in": u.get("input", 0), "out": u.get("output", 0), "cache_read": u.get("cacheRead", 0),
            "cache_write": u.get("cacheWrite", 0), "reasoning": u.get("reasoning", 0), "cost": float(u.get("usd", 0) or 0)}


def add_counters(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    return {k: (a.get(k) or 0) + (b.get(k) or 0) for k in USAGE_KEYS}


class FlowHost:
    """One (space, agent): the engine state, the pending task, the task / discussion spend. Hub calls go through
    `request` (frame -> reply frame, raises on a hub error) and turns through `inject(thread, title, text)`."""

    def __init__(self, fs: FlowSpace, state_dir: Path, request: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
                 inject: Callable[[str, str, str], Awaitable[None]], make_board: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.time, company: Optional[Callable[[], Company]] = None):
        self.fs = fs
        self.path = state_dir / ("flow-%s-%s.json" % (fs.space, fs.agent))
        self.token_dir = state_dir
        self._request, self._inject, self._clock = request, inject, clock
        self._make_board = make_board or self._board
        try:   # the loop the hub calls run on: the engine and the board tools call the GitLab board from other threads
            self._loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self.configs = ConfigStore(state_dir / "config" / fs.space)
        self._company_default = company is None
        self._company = company or (lambda: load_company(self.config_dir()))
        self.state: Dict[str, Any] = self._load()
        self.lock = asyncio.Lock()
        self.task_turn = False            # a task turn of this host is running (no tick meanwhile)
        self.last_tick = 0.0
        self.poll_min: Optional[float] = None  # the agent's `limits.poll_min` in the config, read at each tick
        self.caps: Dict[str, Optional[float]] = {"day": None, "month": None}  # cost caps of the config, read at each tick
        self.reported: Optional[Dict[str, Any]] = None  # last `flow.state` the hub accepted (sent again only on change)
        self.piloted = False              # the hub's last `flow.pilot` for this agent (memory only, § 13)

    # -- state -----------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            s = json.loads(self.path.read_text())
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        os.replace(tmp, self.path)

    @property
    def engine(self) -> Dict[str, Any]:
        return self.state.setdefault("engine", {})

    @property
    def thread(self) -> Optional[str]:
        """The issue thread of the current task, once the hub placed it."""
        return self.state.get("thread")

    def usage(self) -> Dict[str, float]:
        """Cumulative spend in this space, task + discussion: every cost counts towards the quotas."""
        c = self.state.get("counters") or {}
        return add_counters(c.get("task") or ZERO, c.get("discussion") or ZERO)

    def add_usage(self, tag: str, turn_usage: Optional[Dict[str, Any]]) -> None:
        """One turn's spend, tagged. The ledger (30 days) feeds the display; a discussion turn while no task runs also
        goes into the engine's spend, so the quotas count everything (during a task, the engine's running delta
        already includes it)."""
        if not turn_usage:
            return
        c = self.state.setdefault("counters", {})
        c[tag] = add_counters(c.get(tag) or ZERO, to_counters(turn_usage))
        now, cost = int(self._clock()), float(turn_usage.get("usd", 0) or 0)
        self.state["ledger"] = [e for e in self.state.get("ledger") or [] if e[0] >= now - MONTH_S] + [[now, tag, round(cost, 6)]]
        if tag == "discussion" and cost and not self.engine.get("current"):
            self.engine["spend"] = (self.engine.get("spend") or []) + [[now, cost]]
        self.save()

    # -- display and pause (docs/design-flow.md § 4.6) ------------------------------
    def snapshot(self, running: bool = False, alive: Optional[int] = None) -> Dict[str, Any]:
        """What `flow.state` reports: pause, current task, spend today / 30 days per tag against the caps, alerts."""
        now = int(self._clock())
        today = local_day(now)
        led = [e for e in self.state.get("ledger") or [] if e[0] >= now - MONTH_S]
        usage = {tag: {"day": round(sum(c for t, g, c in led if g == tag and local_day(t) == today), 4),
                       "month": round(sum(c for _, g, c in led if g == tag), 4)} for tag in TAGS}
        p = self.engine.get("paused")
        paused = {"by": "quota" if p.get("by") == "quota" else "human", "reason": str(p.get("reason") or "")[:200],
                  "since": int(p.get("ts") or 0)} if isinstance(p, dict) else None
        cur = self.engine.get("current")
        current = {"ref": str(cur.get("ref")), "trigger": str(cur.get("trigger")), "thread": self.thread,
                   "since": int(cur.get("delivered0") or 0), "running": bool(running and self.thread),
                   "alive": int(alive) if alive else None} if isinstance(cur, dict) and cur.get("ref") else None
        alerts = [{"at": int(a.get("ts") or 0), "ref": str(a.get("ref") or "-"), "text": str(a.get("msg") or "")[:300]}
                  for a in (self.engine.get("alerts") or []) if int(a.get("ts") or 0) >= now - ALERTS_AGE_S][-ALERTS_SHOWN:]
        out = {"paused": paused, "current": current, "usage": usage, "caps": dict(self.caps), "alerts": alerts}
        perm = self.state.get("perm")   # ACP bridge: a refused permission blocked a task (cleared on Resume)
        if isinstance(perm, dict) and paused:
            out["perm"] = {"at": int(perm.get("at") or 0), "ref": str(perm.get("ref") or "-"),
                           "what": str(perm.get("what") or "")[:80], "fix": str(perm.get("fix") or "")[:300]}
        return out

    async def set_paused(self, paused: bool, by: str) -> bool:
        """Pause / Resume from briefling. A running turn is not cut; a paused agent takes no new task. Resume also
        lifts a quota pause, for the rest of the day (the cap applies again tomorrow). Returns whether it changed."""
        async with self.lock:
            now = int(self._clock())
            if paused:
                if self.engine.get("paused"):
                    return False
                self.engine["paused"] = {"ts": now, "reason": "paused from briefling by %s" % by, "by": "human"}
            else:
                p = self.engine.pop("paused", None)
                self.state.pop("perm", None)
                if not p:
                    return False
                if isinstance(p, dict) and p.get("by") == "quota":
                    self.engine["quota_waived"] = local_day(now)
            self.save()
            return True

    # -- config ----------------------------------------------------------------
    def config_dir(self) -> Path:
        """The hub's last good bundle, else the local folder of the settings."""
        cur = self.configs.current()
        if cur:
            return cur["dir"]
        if self.fs.config is None:
            raise ConfigError("no flow config yet (none pushed by the hub, no local folder)")
        return self.fs.config

    def has_config(self) -> bool:
        return self.fs.config is not None or self.configs.current() is not None

    def apply_config(self, version: int, sha: str, files: Any) -> tuple:
        """A bundle pushed by the hub: (ok, error). Refused = the previous one stays in use."""
        return self.configs.apply(version, sha, files)

    # -- GitHub ----------------------------------------------------------------
    def company(self) -> Company:
        return self._company()

    def _board(self, co: Company, dept: Department):
        """`board.type: gitlab` = the space's GitLab board, read and written by the hub; else a GitHub Project."""
        if str(dept.board.get("type") or "") == "gitlab":
            return GitLabBoard(self.fs.space, self.fs.agent, dept.flow, bots(co), self.hub_call)
        return self._github_board(co, dept)

    def _github_board(self, co: Company, dept: Department):
        token = make_token(self.fs.identity, dept.board["owner"], cache_dir=self.token_dir) if self.fs.identity else None
        # Writers: the trusted bots that are not agent identities (the hub's App): only they may carry an approval marker.
        agents = {str(a.get("identity", "")).lower() for a in co.agents.values() if a.get("identity")}
        return GitHubBoard(dept.board, dept.flow, bots(co), token=token, writers=set(co.trusted_bots) - agents)

    def hub_call(self, frame: Dict[str, Any], timeout: float = HUB_CALL_S) -> Dict[str, Any]:
        """A hub frame from a worker thread (the tick's engine, a board tool): run on the host's loop, waited here.
        A hub refusal raises GitLabError(code), so the agent and the logs see why (never a token: the hub keeps it)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            raise GitLabError("briefling hub not reachable")
        try:
            if asyncio.get_running_loop() is loop:
                raise GitLabError("hub call from the event loop thread")   # would deadlock: a bug, never silent
        except RuntimeError:
            pass
        fut = asyncio.run_coroutine_threadsafe(self._request(frame), loop)
        try:
            return fut.result(timeout)
        except HubAnswer as e:
            raise GitLabError("hub refused %s: %s" % (frame.get("type"), e)) from e
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            fut.cancel()
            raise GitLabError("briefling hub not reachable (%s)" % type(e).__name__) from e

    def boards(self, co: Company) -> Dict[str, Any]:
        return {d.name: self._make_board(co, d) for d in co.departments_of(self.fs.agent)}

    # -- tick ------------------------------------------------------------------
    def _poll(self, alive: Optional[int]) -> str:
        co = self.company()
        limits = co.limits_for(self.fs.agent)
        pm = limits.get("poll_min")
        self.poll_min = float(pm) if isinstance(pm, (int, float)) and pm > 0 else None
        self.caps = {k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
                     for k, v in (("day", limits.get("max_cost_per_day")), ("month", limits.get("max_cost_per_month")))}
        return poll(self.fs.agent, co, self.boards(co), self.engine, now=int(self._clock()), usage=self.usage, alive=alive)

    async def tick(self, alive: Optional[int] = None) -> str:
        """One pass: a pending task first, else the engine. Returns what happened (logs, tests)."""
        if self.task_turn or self.lock.locked():
            return "busy"
        async with self.lock:
            if not self.piloted:               # second gate: unstrikeable (or nobody) pilots this agent
                return "not piloted"
            self.last_tick = self._clock()
            await self._flush()
            if self.state.get("pending"):
                if self.engine.get("paused"):      # paused after the engine picked it: it waits for Resume
                    return "paused"
                return await self._start()
            if self._company_default and not self.has_config():
                return "no_config"
            out = await asyncio.to_thread(self._poll, alive)
            finished = self.engine.pop("finished", None) or []
            self.save()
            for rec in finished:
                await self._done(rec)
            if not out:
                return "paused" if self.engine.get("paused") else "idle"
            cur = self.engine.get("current") or {}
            self.state["pending"] = {"key": cur.get("key"), "ref": cur.get("ref"), "trigger": cur.get("trigger"),
                                     "title": cur.get("title") or cur.get("ref"), "text": out}
            self.save()
            return await self._start()

    async def _start(self) -> str:
        p = self.state["pending"]
        cur = self.engine.get("current") or {}
        if cur.get("key") != p["key"]:          # the engine moved on (task closed meanwhile): drop it
            self.state.pop("pending", None)
            self.save()
            return "dropped"
        try:
            r = await self._request({"type": "flow.task", "space": self.fs.space, "agent": self.fs.agent, "ref": p["ref"],
                                     "key": p["key"], "trigger": p["trigger"], "title": (p["title"] or "")[:300]})
        except Exception as e:  # hub refusal or unreachable: the task stays pending, retried at the next tick
            logger.warning("briefling flow: task %s not placed: %s", p["ref"], e)
            return "refused"
        if r.get("status") != "started" or not r.get("thread"):
            return str(r.get("status") or "refused")
        if self.state.get("task_key") != p["key"]:  # a new task (not a retry/resume of the same one): its spend starts here
            self.state["task_key"], self.state["task_usage0"] = p["key"], (self.state.get("counters") or {}).get("task") or ZERO
        self.state["thread"] = r["thread"]
        cur["delivered"] = int(self._clock())   # the ack timer starts when the turn really starts
        self.state.pop("pending", None)
        self.task_turn = True
        self.save()
        try:
            await self._inject(r["thread"], p["title"] or p["ref"], p["text"])
        except Exception:
            self.task_turn = False
            raise
        return "started"

    def turn_ended(self) -> None:
        self.task_turn = False

    async def _done(self, rec: Dict[str, Any]) -> None:
        # The engine closes only its current task, the one placed last: its task-tagged spend since it was placed.
        # No usage ever recorded (an ACP agent reports none): cost unknown, never a $0 the Board would show as such.
        task = (self.state.get("counters") or {}).get("task")
        spent = usage_delta(self.state.get("task_usage0"), task) if self.state.get("task_key") and task else None
        record = {"outcome": rec.get("outcome") if rec.get("outcome") in ("done", "blocked", "gone", "lost") else "gone",
                  "kind": rec.get("kind"), "trigger": rec.get("trigger"), "start": int(rec.get("start") or 0),
                  "end": int(rec.get("end") or 0), "retries": int(rec.get("retries") or 0), "runs": int(rec.get("runs") or 0),
                  "reviews": int(rec.get("reviews") or 0), "cost": round(spent["cost"], 4) if spent else None}
        frame = {"type": "flow.done", "space": self.fs.space, "agent": self.fs.agent, "ref": rec.get("ref"),
                 "key": self.state.get("task_key") or str(rec.get("ref")), "record": record}
        for k in ("thread", "task_key", "task_usage0"):
            self.state.pop(k, None)
        self.state["outbox"] = (self.state.get("outbox") or [])[-49:] + [frame]
        self.save()
        await self._flush()

    async def _flush(self) -> None:
        """Closed tasks not acknowledged by the hub yet (unreachable): sent again, in order, until it answers.
        Why: the hub keeps the agent's hand on the thread until it gets them."""
        while self.state.get("outbox"):
            frame = self.state["outbox"][0]
            try:
                await self._request(frame)
            except HubAnswer as e:   # refused for good (bad frame, space changed): dropped, never retried forever
                logger.warning("briefling flow: record of %s refused by the hub: %s", frame.get("ref"), e)
            except Exception as e:
                logger.warning("briefling flow: record of %s not stored yet: %s", frame.get("ref"), type(e).__name__)
                return
            self.state["outbox"].pop(0)
            self.save()

    # -- switch by link (docs/design-flow.md § 13) ---------------------------------
    async def switch(self, code: str) -> Dict[str, Any]:
        """Import unstrikeable's state of this agent, pause it, redeem the code at the hub; accepted = piloted.
        Raises SwitchError (nothing changed, the file restored when we had paused it)."""
        async with self.lock:
            path = flowswitch.uns_state_path(self.fs.agent, self.fs.uns_state)
            now = int(self._clock())
            original, raw = await asyncio.to_thread(flowswitch.read_state, path)
            imp = flowswitch.extract(raw, now)
            cur = raw.get("current")
            if cur is not None and not isinstance(cur, dict):
                raise SwitchError("unstrikeable's state file is malformed (current): nothing was changed")
            if cur:
                raise SwitchError("a task is in progress in unstrikeable (%s): let it finish (uns closes it at its "
                                  "next poll), then retry" % str(cur.get("ref") or "?")[:200])
            rewrote = not flowswitch.paused_by_briefling(raw)   # already paused by us: a lost answer, redeem again
            if rewrote:
                try:
                    await asyncio.to_thread(flowswitch.write_atomic, path, flowswitch.pause_text(raw, now))
                except OSError as e:
                    raise SwitchError("cannot pause unstrikeable's state file %s: %s" % (path, type(e).__name__))
            imported = flowswitch.counters(imp)
            frame = {"type": "flow.switch", "space": self.fs.space, "agent": self.fs.agent, "code": code,
                     "imported": imported}
            try:
                await self._request(frame)
            except (HubAnswer, ConnectionError) as e:   # refused or never sent: unstrikeable goes on as before
                if rewrote:
                    await self._restore(path, original)
                code_ = str(e)
                if isinstance(e, HubAnswer) and code_ == "bad_code":
                    raise SwitchError("link unknown, used or expired: ask an admin for a new one")
                raise SwitchError("switch refused by the hub: %s" % code_ if isinstance(e, HubAnswer)
                                  else "briefling hub not reachable: retry when connected")
            except asyncio.TimeoutError:   # the hub may have taken it: keep the pause, a retry redeems again
                raise SwitchError("no answer from the hub: unstrikeable stays paused; run flow_switch again with "
                                  "the same link")
            added = flowswitch.merge(self.engine, imp, now)
            # the Board block's today / 30-day spend reads the ledger: unstrikeable's board spend counts as task
            self.state["ledger"] = [e for e in self.state.get("ledger") or [] if e[0] >= now - MONTH_S] + \
                [[int(t), "task", round(float(c), 6)] for t, c in added]
            self.state["switched"] = {"from": str(path), "at": now}
            self.save()
            self.piloted = True
            logger.info("briefling flow %s/%s switched from unstrikeable (%s): %s", self.fs.space, self.fs.agent,
                        path, imported)
            return {"ok": True, "space": self.fs.space, "agent": self.fs.agent, "imported": imported,
                    "state_file": str(path), "paused": True, "next": "remove your uns_poll cron job if not done yet"}

    async def _restore(self, path: Path, original: bytes) -> None:
        try:
            await asyncio.to_thread(flowswitch.write_atomic, path, original)
        except OSError as e:
            logger.warning("briefling flow %s/%s: unstrikeable's state %s not restored: %s", self.fs.space,
                           self.fs.agent, path, type(e).__name__)

    async def switch_back(self) -> bool:
        """« Back to unstrikeable » (`flow.pilot false` after a switch): the engine state goes back to the file it
        came from, without briefling's pause. Returns whether it was written; a failure keeps `switched`."""
        async with self.lock:
            sw = self.state.get("switched")
            if not isinstance(sw, dict) or not sw.get("from"):
                return False
            path = Path(str(sw["from"]))
            if path.name != flowswitch.state_name(self.fs.agent):
                logger.warning("briefling flow %s/%s: switched.from %s is not a %s file, not written back",
                               self.fs.space, self.fs.agent, path, flowswitch.state_name(self.fs.agent))
                return False
            try:
                await asyncio.to_thread(flowswitch.write_atomic, path, flowswitch.back_text(self.engine))
            except OSError as e:
                logger.warning("briefling flow %s/%s: state not written back to %s: %s", self.fs.space, self.fs.agent,
                               path, type(e).__name__)
                return False
            self.state.pop("switched", None)
            self.save()
            logger.info("briefling flow %s/%s: back to unstrikeable, state written to %s", self.fs.space,
                        self.fs.agent, path)
            return True

    # -- board tools (agent's own identity, its departments' repos only) -----------
    def _target(self, ref: str):
        co = self.company()
        dept = department_of(co, self.fs.agent, ref)
        return co, dept, self._make_board(co, dept)

    def board_status(self, ref: str, state: str, todo: str = "", done: str = "", note: str = "", kind: str = "",
                     learned: str = "") -> str:
        if state not in ("working", "done", "blocked"):
            raise FlowError("state must be working, done or blocked")
        lesson = (learned or "").strip()
        if state == "done" and not lesson:
            raise FlowError('closing a task needs a lesson: learned="<rule> because <reason>" (or "none")')
        _, dept, board = self._target(ref)
        kinds = dept.flow.kinds
        if kind and kind not in kinds:
            raise FlowError("unknown kind %r (flow %s: %s)" % (kind, dept.flow.name, ", ".join(kinds) or "no kinds"))
        if state == "done" and kinds and not kind:
            raise FlowError("closing a task needs its kind: one of %s" % "|".join(kinds))
        it = board.item(ref)
        if it is None:
            raise FlowError("%s is closed or not on the board" % ref)
        prev = None
        for c in it.comments:
            prev = prev or (parse_status(c.body, self.fs.agent) if c.status else None)
        now = int(self._clock())
        since = prev["since"] if prev and (prev["state"] == "working" or state != "working") else now
        board.upsert_status(ref, self.fs.agent, status_body(self.fs.agent, state, since, now, done, todo, note, kind=kind))
        if state == "done" and lesson.lower() not in NOTHING_LEARNED:
            self.state.setdefault("learned", []).append({"ref": ref, "at": now, "lesson": lesson[:1000]})
            self.state["learned"] = self.state["learned"][-50:]
            self.save()
        return "%s status %s -> %s" % (ref, self.fs.agent, state)

    def board_comment(self, ref: str, body: str) -> str:
        if not (body or "").strip():
            raise FlowError("empty comment")
        _, _, board = self._target(ref)
        board.comment(ref, body.rstrip() + "\n\n" + AGENT_MARK % self.fs.agent)
        return "commented on %s" % ref

    def board_move(self, ref: str, state: str, force: bool = False) -> str:
        _, dept, board = self._target(ref)
        if not dept.flow.has_state(state):
            raise FlowError("unknown state %r (states: %s)" % (state, ", ".join(s.key for s in dept.flow.states)))
        it = board.item(ref)
        if it is None:
            raise FlowError("%s is closed or not on the board" % ref)
        if state in ("ready", "doing") and it.blocked_by and not force:
            raise FlowError("%s is blocked by %d open item(s); refusing (force only if a human said so)" % (ref, it.blocked_by))
        board.move(ref, dept.flow.column(state))
        return "%s -> %s (%s)" % (ref, state, dept.flow.column(state))

    def board_set(self, ref: str, field: str, value: str) -> str:
        """A single-select field of the board (Size, Priority...), like `uns set`."""
        if not field.strip() or not value.strip():
            raise FlowError("field and value are required")
        _, _, board = self._target(ref)
        board.set_field(ref, field.strip(), value.strip())
        return "%s %s = %s" % (ref, field.strip(), value.strip())

    def board_label(self, ref: str, add: List[str], remove: List[str]) -> str:
        _, dept, board = self._target(ref)
        for label in [*add, *remove]:
            if any(r.agent_of(label) or label in r.pool_labels() for r in dept.flow.roles.values()):
                raise FlowError("%s: assignment labels are set by humans" % label)
        board.labels(ref, add=add, remove=remove)
        return "%s labels +%s -%s" % (ref, ",".join(add) or "-", ",".join(remove) or "-")
