"""Switch by link (docs/design-flow.md § 13): an agent's board work moves from unstrikeable to briefling.

Pure helpers, no hub call: parse the link an admin gave, find unstrikeable's state file of the agent (from the
operator's settings only), validate and extract what the engine imports, pause / restore that file, merge the import
into the engine state, and write the engine state back on « Back to unstrikeable ». The orchestration (lock, hub
request) is ``FlowHost.switch`` / ``FlowHost.switch_back``.

The state file is the agent's own data on this machine: nothing of it goes to the hub or the logs beyond three
counters (events seen, items with runs, 30-day spend).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

MONTH_S = 30 * 86400
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")              # AGENT_CODE_BYTES = 24, base64url
SPACE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")   # protocol/frames.ts SPACE_ID_RE
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")                      # protocol/frames.ts AGENT_RE
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SEEN, MAX_KEY = 5000, 300
MAX_REFS = 2000
MAX_ALERTS, MAX_ALERT_MSG = 200, 500
PAUSE_BY = "briefling"
PAUSE_REASON = "piloted by briefling"


class SwitchError(Exception):
    """Refusal shown to the agent as the tool's error."""


def parse_link(link: str) -> Tuple[str, str, str]:
    """(code, space, agent) from `<origin>/agent.md?switch=<code>&space=<space>&agent=<agent>`."""
    try:
        u = urlparse(str(link or "").strip())
    except ValueError:
        raise SwitchError("not a switch link")
    q = parse_qs(u.query, keep_blank_values=True)

    def one(name: str) -> str:
        v = q.get(name) or []
        if len(v) != 1:
            raise SwitchError("not a switch link: it needs exactly one %s=" % name)
        return v[0]

    if u.scheme not in ("http", "https") or not u.netloc:
        raise SwitchError("not a switch link: paste the whole link an admin gave you")
    code, space, agent = one("switch"), one("space"), one("agent")
    if not CODE_RE.match(code):
        raise SwitchError("the link's code is malformed: ask an admin for a new link")
    if not SPACE_ID_RE.match(space):
        raise SwitchError("the link's space is malformed")
    if not AGENT_RE.match(agent):
        raise SwitchError("the link's agent is malformed")
    return code, space, agent


def state_name(agent: str) -> str:
    return "poll-%s.json" % agent


def uns_state_path(agent: str, configured: Optional[Path]) -> Path:
    """unstrikeable's poll state of `agent` as this gateway sees it: the settings' `uns_state`, else
    $UNS_HOME/state/, else ~/.unstrikeable/state/. Never from a tool argument."""
    if configured is not None:
        p = Path(os.path.expanduser(str(configured)))
        if p.name != state_name(agent):
            raise SwitchError("uns_state in the BRIEFLING_FLOW settings must be a file named %s (got %s)"
                              % (state_name(agent), p.name))
        return p
    home = os.environ.get("UNS_HOME")
    base = Path(os.path.expanduser(home)) if home else Path.home() / ".unstrikeable"
    return base / "state" / state_name(agent)


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def extract(raw: Dict[str, Any], now: int) -> Dict[str, Any]:
    """What the engine imports from unstrikeable's state, validated (types, sizes); raises SwitchError on anything
    malformed. Never `current` nor `paused`."""
    def bad(what: str) -> SwitchError:
        return SwitchError("unstrikeable's state file is malformed (%s): nothing was changed" % what)

    seen = raw.get("seen") or []
    if not isinstance(seen, list) or not all(isinstance(k, str) and 0 < len(k) <= MAX_KEY for k in seen):
        raise bad("seen")
    out: Dict[str, Any] = {"seen": sorted(set(seen))[-MAX_SEEN:]}
    for name in ("runs", "reviews"):
        d = raw.get(name) or {}
        if not isinstance(d, dict) or len(d) > MAX_REFS or not all(
                isinstance(k, str) and 0 < len(k) <= MAX_KEY and _int(v) and v >= 0 for k, v in d.items()):
            raise bad(name)
        out[name] = dict(d)
    spend = raw.get("spend") or []
    if not isinstance(spend, list) or not all(
            isinstance(s, list) and len(s) == 2 and _num(s[0]) and _num(s[1]) and s[1] >= 0 for s in spend):
        raise bad("spend")
    out["spend"] = [[s[0], s[1]] for s in spend if s[0] >= now - MONTH_S]
    day, count = raw.get("day"), raw.get("day_count")
    if day is not None and not (isinstance(day, str) and DAY_RE.match(day)):
        raise bad("day")
    if count is not None and not (_int(count) and count >= 0):
        raise bad("day_count")
    if day is not None:
        out["day"], out["day_count"] = day, count or 0
    alerts = raw.get("alerts") or []
    if not isinstance(alerts, list) or not all(
            isinstance(a, dict) and _int(a.get("ts")) and isinstance(a.get("ref"), str) and len(a["ref"]) <= MAX_KEY
            and isinstance(a.get("msg"), str) and len(a["msg"]) <= MAX_ALERT_MSG for a in alerts):
        raise bad("alerts")
    out["alerts"] = [{"ts": a["ts"], "ref": a["ref"], "msg": a["msg"]} for a in alerts][-MAX_ALERTS:]
    return out


def counters(imp: Dict[str, Any]) -> Dict[str, Any]:
    """The only thing the hub learns of the import (FlowImported)."""
    return {"seen": len(imp["seen"]), "refs": len(set(imp["runs"]) | set(imp["reviews"])),
            "spent": round(sum(c for _, c in imp["spend"]), 4)}


def read_state(path: Path) -> Tuple[bytes, Dict[str, Any]]:
    """(original bytes, parsed dict) of unstrikeable's state file."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SwitchError("unstrikeable's state file %s is too large" % path)
        text = path.read_bytes()
    except FileNotFoundError:
        raise SwitchError("unstrikeable's state file not found: %s (set uns_state of this space in the "
                          "BRIEFLING_FLOW settings to the path of %s, then restart the gateway)" % (path, path.name))
    except OSError as e:
        raise SwitchError("unstrikeable's state file %s unreadable: %s" % (path, type(e).__name__))
    try:
        raw = json.loads(text)
    except ValueError:
        raise SwitchError("unstrikeable's state file is malformed (not JSON): nothing was changed")
    if not isinstance(raw, dict):
        raise SwitchError("unstrikeable's state file is malformed (not an object): nothing was changed")
    return text, raw


def write_atomic(path: Path, data: bytes) -> None:
    """tmp + os.replace in the same folder, keeping the file's mode when it exists."""
    try:
        mode: Optional[int] = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = None
    tmp = path.with_name(".%s.briefling.tmp" % path.name)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def paused_by_briefling(raw: Dict[str, Any]) -> bool:
    p = raw.get("paused")
    return isinstance(p, dict) and p.get("by") == PAUSE_BY


def pause_text(raw: Dict[str, Any], now: int) -> bytes:
    """The original state + a briefling pause: a stray `uns resume` then delivers only new events."""
    return json.dumps({**raw, "paused": {"ts": now, "by": PAUSE_BY, "reason": PAUSE_REASON}}, indent=1).encode()


def merge(engine: Dict[str, Any], imp: Dict[str, Any], now: int) -> List[List[Any]]:
    """The import into the engine state, in place: nothing delivered twice, no quota reset. Returns the imported
    spend entries the engine did not hold yet (for the display ledger)."""
    held = {(s[0], s[1]) for s in engine.get("spend") or []}
    added = sorted({(s[0], s[1]) for s in imp["spend"] if s[0] >= now - MONTH_S} - held)
    engine["seen"] = sorted(set(engine.get("seen") or []) | set(imp["seen"]))[-MAX_SEEN:]
    for name in ("runs", "reviews"):
        d = dict(engine.get(name) or {})
        for k, v in imp[name].items():
            d[k] = max(int(d.get(k) or 0), v)
        engine[name] = d
    spend = {(s[0], s[1]) for s in (engine.get("spend") or []) + imp["spend"] if s[0] >= now - MONTH_S}
    engine["spend"] = [[t, c] for t, c in sorted(spend)]
    if "day" in imp:
        mine = engine.get("day")
        if mine == imp["day"]:
            engine["day_count"] = max(int(engine.get("day_count") or 0), imp["day_count"])
        elif not isinstance(mine, str) or imp["day"] > mine:
            engine["day"], engine["day_count"] = imp["day"], imp["day_count"]
    alerts = list(engine.get("alerts") or [])
    known = {(a.get("ts"), a.get("ref"), a.get("msg")) for a in alerts if isinstance(a, dict)}
    alerts += [a for a in imp["alerts"] if (a["ts"], a["ref"], a["msg"]) not in known]
    engine["alerts"] = sorted(alerts, key=lambda a: int(a.get("ts") or 0) if isinstance(a, dict) else 0)[-MAX_ALERTS:]
    return [[t, c] for t, c in added]


def back_text(engine: Dict[str, Any]) -> bytes:
    """The engine state for unstrikeable on « Back to unstrikeable »: same keys, without briefling's pause (a task
    in progress stays as `current`)."""
    out = dict(engine)
    if paused_by_briefling(out):
        out.pop("paused")
    return json.dumps(out, indent=1).encode()
