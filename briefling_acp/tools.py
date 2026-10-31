"""briefling's agent tools on the bridge side: which ones the agent gets, how each call becomes a hub frame, and how
the hub's answer becomes the tool result. Schemas, limits and refusal texts come from tooldefs.py, the Hermes plugin's
own file, so the agent reads exactly what a Hermes agent reads; the checks below mirror the plugin's tool handlers.
The hub applies every gate again (turn, membership, space settings): these checks only give the agent a clear
message before a frame is sent.

Not offered: the board/flow tools (the flow engine lives in the Hermes plugin) and memory_review/memory_compact
(turns the hub starts for a designated Hermes agent).
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

from . import tooldefs as td

SCHEMAS = [td.OUTCOME_SCHEMA, td.PROPOSE_SCHEMA, td.PLAN_SCHEMA, td.ISSUE_SCHEMA, td.ABOUT_SCHEMA, td.ACQUIRE_SCHEMA,
           td.RELEASE_SCHEMA, td.PUSHED_SCHEMA, td.MEMORY_SEARCH_SCHEMA, td.MEMORY_READ_SCHEMA, td.MEMORY_PROPOSE_SCHEMA,
           td.TOPIC_TITLE_SCHEMA]
NAMES = [s["name"] for s in SCHEMAS]

Built = Tuple[Optional[Dict[str, Any]], Optional[str]]  # (frame without `thread`, None) or (None, message)


def mcp_tools() -> List[Dict[str, Any]]:
    """The tools as MCP `tools/list` entries."""
    return [{"name": s["name"], "description": s["description"], "inputSchema": s["parameters"]} for s in SCHEMAS]


def _s(args: Dict[str, Any], key: str) -> str:
    return str(args.get(key) or "").strip()


def _one_line(args: Dict[str, Any], key: str) -> str:
    return " ".join(str(args.get(key) or "").split())


def _space(args: Dict[str, Any], frame: Dict[str, Any]) -> Optional[str]:
    raw = args.get("space")
    if raw is None or str(raw).strip() == "":
        return None
    if not td.ID_RE.match(str(raw).strip()):
        return "invalid space id."
    frame["space"] = str(raw).strip()
    return None


def build_outcome(args: Dict[str, Any]) -> Built:
    outcome, action = _s(args, "outcome"), _one_line(args, "action")
    if outcome not in ("done", "waiting"):
        return None, "outcome must be 'done' or 'waiting'."
    if outcome == "waiting" and not action:
        return None, "action is required for 'waiting': what must the human do?"
    if len(action) > td.MAX_ACTION:
        return None, f"action too long ({len(action)} chars, max {td.MAX_ACTION}): one short line."
    summary, err = td.clean_summary(args.get("summary"))
    if err:
        return None, err
    return {"type": "thread.outcome", "outcome": outcome, **({"action": action} if outcome == "waiting" else {}),
            **({"summary": summary} if summary else {})}, None


def build_propose(args: Dict[str, Any]) -> Built:
    title, brief, why = _s(args, "title"), _s(args, "brief"), _s(args, "why")
    if not title or not brief:
        return None, "title and brief are required."
    if len(brief) > td.MAX_BRIEF:
        return None, f"brief too long ({len(brief)} chars, max {td.MAX_BRIEF}): keep 3 to 5 lines."
    return {"type": "thread.propose", "title": title[:120], "brief": brief, **({"why": why[:500]} if why else {})}, None


def build_issue(args: Dict[str, Any]) -> Built:
    repo, title, body = _s(args, "repo"), _one_line(args, "title"), _s(args, "body")
    if not td.REPO_RE.fullmatch(repo):
        return None, "repo is required, as owner/name (one of the repos listed in the message context)."
    if not title or len(title) > td.NEW_ISSUE_TITLE_MAX:
        return None, f"title is required, one line, max {td.NEW_ISSUE_TITLE_MAX} chars."
    if len(body) > td.NEW_ISSUE_BODY_MAX:
        return None, f"body too long ({len(body)} chars, max {td.NEW_ISSUE_BODY_MAX})."
    return {"type": "issue.propose", "repo": repo, "title": title, "body": body}, None


def build_about(args: Dict[str, Any]) -> Built:
    text, why = _s(args, "text"), _s(args, "why")
    if not text:
        return None, "text is required: the full proposed description."
    if len(text) > td.MAX_ABOUT:
        return None, f"description too long ({len(text)} chars, max {td.MAX_ABOUT}): keep it short, it is read on every turn."
    return {"type": "about.propose", "text": text, **({"why": why[:500]} if why else {})}, None


def build_title(args: Dict[str, Any]) -> Built:
    title = _one_line(args, "title")
    if not title:
        return None, "title is required: one short line."
    if len(title) > td.TOPIC_HEADING_MAX:
        return None, f"title too long ({len(title)} chars, max {td.TOPIC_HEADING_MAX}): shorten it."
    return {"type": "topic.title", "title": title}, None


def _resource(kind: str) -> Callable[[Dict[str, Any]], Built]:
    def build(args: Dict[str, Any]) -> Built:
        resource = _s(args, "resource")
        if not td.RESOURCE_RE.fullmatch(resource):
            return None, (f"invalid resource name {resource[:60]!r}: use a name listed in the conversation context "
                          "(e.g. 'emulator', 'stack:briefling-dev').")
        return {"type": kind, "resource": resource}, None
    return build


def build_pushed(args: Dict[str, Any]) -> Built:
    raw = args.get("commits")
    if not isinstance(raw, list) or not 1 <= len(raw) <= td.CI_MAX_COMMITS:
        return None, f"commits is required: 1 to {td.CI_MAX_COMMITS} items {{repo, sha}}."
    commits = []
    for c in raw:
        c = c if isinstance(c, dict) else {}
        repo, sha, branch = _s(c, "repo"), _s(c, "sha").lower(), _s(c, "branch")
        if not td.REPO_RE.fullmatch(repo) or not td.SHA_RE.fullmatch(sha):
            return None, "each commit needs repo (owner/name) and the full 40-character sha (git rev-parse HEAD)."
        if branch and not td.BRANCH_RE.fullmatch(branch):
            return None, "branch must be a plain git branch name (letters, digits, . _ / -), as git branch --show-current prints it."
        commits.append({"repo": repo, "sha": sha, **({"branch": branch} if branch else {})})
    return {"type": "ci.watch", "commits": commits}, None


def build_search(args: Dict[str, Any]) -> Built:
    q = _one_line(args, "query")
    if len(q) > td.MEMORY_Q_MAX:
        return None, f"query too long (max {td.MEMORY_Q_MAX} chars)."
    frame: Dict[str, Any] = {"type": "memory.search", "q": q}
    err = _space(args, frame)
    return (None, err) if err else (frame, None)


def build_read(args: Dict[str, Any]) -> Built:
    entry = _s(args, "id")
    if not td.ID_RE.match(entry):
        return None, "id is required: an entry id from memory_search."
    frame: Dict[str, Any] = {"type": "memory.read", "id": entry}
    err = _space(args, frame)
    return (None, err) if err else (frame, None)


def build_memory_propose(args: Dict[str, Any]) -> Built:
    text, kind, raw = _s(args, "text"), _s(args, "kind"), args.get("sources")
    if not text or len(text) > td.MEMORY_TEXT_MAX:
        return None, f"text is required, max {td.MEMORY_TEXT_MAX} chars: one short fact."
    if kind not in ("fact", "rule"):
        return None, "kind must be 'fact' or 'rule'."
    if not isinstance(raw, list) or not 1 <= len(raw) <= td.MEMORY_SOURCES_MAX:
        return None, f"sources is required: 1 to {td.MEMORY_SOURCES_MAX} items (file path, issue, URL, 'this thread')."
    sources = [" ".join(str(x or "").split()) for x in raw]
    if any(not x or len(x) > td.MEMORY_SOURCE_MAX for x in sources):
        return None, f"each source: 1 to {td.MEMORY_SOURCE_MAX} chars."
    return {"type": "memory.propose", "text": text, "kind": kind, "sources": sources}, None


BUILD: Dict[str, Callable[[Dict[str, Any]], Built]] = {
    "turn_outcome": build_outcome, "propose_thread": build_propose, "propose_issue": build_issue, "propose_plan": td.build_plan,
    "propose_space_about": build_about, "acquire_resource": _resource("resource.acquire"),
    "release_resource": _resource("resource.release"), "pushed_commits": build_pushed, "memory_search": build_search,
    "memory_read": build_read, "memory_propose": build_memory_propose, "topic_title": build_title,
}
assert set(BUILD) == set(NAMES)

ERRORS = {
    "turn_outcome": td.OUTCOME_ERRORS, "propose_thread": td.PROPOSE_ERRORS, "propose_issue": td.ISSUE_ERRORS, "propose_plan": td.PLAN_ERRORS,
    "propose_space_about": td.ABOUT_ERRORS, "acquire_resource": td.RESOURCE_ERRORS, "release_resource": td.RESOURCE_ERRORS,
    "pushed_commits": td.CI_ERRORS, "memory_search": td.MEMORY_ERRORS, "memory_read": td.MEMORY_ERRORS,
    "memory_propose": td.MEMORY_ERRORS, "topic_title": td.TITLE_ERRORS,
}


def refused(tool: str, code: str) -> Dict[str, Any]:
    """The hub refused the frame (`error` with the frame's client_id as `ref`)."""
    return {"ok": False, "error": code, "detail": ERRORS[tool].get(code, code)}


def result(tool: str, frame: Dict[str, Any], answer: Dict[str, Any]) -> Dict[str, Any]:
    """The tool result for the hub's answer (ack, resource.result, memory.result, memory.proposed): same as the plugin."""
    if tool == "turn_outcome":
        if frame["outcome"] == "waiting":
            return {"ok": True, "detail": "Shown in the thread: the human sees what you wait for."}
        return {"ok": True, "detail": "Thread marked as finished.", "memory": td.MEMORY_NUDGE}
    if tool in td.CARD_SHOWN:
        return {"ok": True, "card": answer.get("msg_id"), "detail": td.CARD_SHOWN[tool]}
    if tool == "acquire_resource":
        resource = frame["resource"]
        if answer.get("granted") is True:
            return {"ok": True, "granted": True,
                    "detail": f"You hold {resource} until you call release_resource or your turn ends. Use it now."}
        holder = str(answer.get("holder") or "another thread")[:120]
        position = answer.get("position") if isinstance(answer.get("position"), int) else None
        where = f" (you are #{position} in the queue)" if position else ""
        return {"ok": True, "granted": False, "holder": holder, "position": position, "detail": (
            f'{resource} is in use by thread "{holder}"{where}. Do NOT use it. Stop here: end your turn now with a '
            f"one-line message saying what you wait for, and do NOT call turn_outcome. The hub resumes this thread "
            f"with a new message when {resource} is yours.")}
    if tool == "topic_title":
        return {"ok": True, "detail": "Title shown in the Lounge list. Nothing else to do for it."}
    if tool == "release_resource":
        return {"ok": True, "detail": f"{frame['resource']} released."}
    if tool == "pushed_commits":
        return {"ok": True, "detail": "Recorded: listed in the thread info (CI result shown in this thread when the space "
                                      "follows it). Nothing else to do."}
    if tool in ("memory_search", "memory_read"):
        keep = ("id", "space", "text", "kind", "sources", "author", "thread", "at")
        entries = [{k: e.get(k) for k in keep} for e in (answer.get("entries") or []) if isinstance(e, dict)]
        return {"ok": True, "space": answer.get("space"), "note": td.MEMORY_DATA_NOTE, "entries": entries}
    if tool == "memory_propose":
        return {"ok": True, "id": answer.get("id"), "status": answer.get("status"),
                "detail": td.MEMORY_GATES.get(str(answer.get("gate") or ""), "Recorded.")}
    raise KeyError(tool)
