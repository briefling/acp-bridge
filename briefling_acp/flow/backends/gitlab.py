"""GitLab board, through the briefling hub (frames ``board.read`` / ``board.write``, protocol/flowboard.ts).

The hub calls GitLab with the space's token, which never reaches this machine: the engine gets the issues (column =
the first board list label, notes, merge requests that close the issue with their merge status and head pipeline, who
is trusted) and asks the hub for each write (column, labels, note, status note edited in place).

``call(frame) -> reply`` is synchronous: the engine runs in a worker thread (the tick) or in the agent's tool thread,
the host bridges it to its event loop (``FlowHost._hub_call``). A hub refusal raises ``GitLabError`` with its code.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Sequence

from ..config import Flow
from ..model import PR, Comment, Item

AGENT_RE = re.compile(r"<!-- (?:uns|gha):agent=(\S+) -->")
STATUS_RE = re.compile(r"<!-- (?:uns|gha):status ")
REF_RE = re.compile(r"^(.{3,255})#([1-9][0-9]{0,8})$")


class GitLabError(Exception):
    pass


def split_ref(ref: str) -> tuple[str, int]:
    m = REF_RE.match(ref or "")
    if not m or "/" not in m.group(1):
        raise GitLabError("bad item ref %r (expected group/project#123)" % ref)
    return m.group(1), int(m.group(2))


def parse_note(n: dict, bots: set[str]) -> Comment:
    login = str(n.get("author") or "").lower()
    trusted = bool(n.get("trusted"))
    body = str(n.get("body") or "")
    m = AGENT_RE.search(body) if trusted else None
    return Comment(int(n["id"]), login or "?", trusted, agent=m.group(1) if m else None,
                   status=trusted and bool(STATUS_RE.search(body)), body=body, created=str(n.get("created") or ""),
                   bot=login in bots)


def outsider_at(it: dict) -> str:
    """Newest content written by a non-member: the issue, its notes, the MRs that close it."""
    xs = [it] + list(it.get("notes") or []) + list(it.get("mrs") or [])
    return max((str(x.get("created") or "") for x in xs if not x.get("trusted")), default="")


def parse_item(it: dict, flow: Flow, bots: set[str]) -> Item:
    author = str(it.get("author") or "").lower()
    prs = [PR(int(p["number"]), str(p.get("url") or ""), str(p.get("head") or ""), str(p.get("mergeable") or "UNKNOWN"), p.get("ci"))
           for p in it.get("mrs") or []]
    return Item(repo=str(it["repo"]), number=int(it["number"]), title=str(it.get("title") or ""),
                state=flow.state_of(it.get("column")), labels=[str(l) for l in it.get("labels") or []], url=str(it.get("url") or ""),
                author=author, author_trusted=bool(it.get("trusted")), author_agent=author if author in bots else None,
                blocked_by=0, comments=[parse_note(n, bots) for n in it.get("notes") or []], prs=prs, outsider_at=outsider_at(it),
                # The hub sets it only on an issue its token wrote from a card a human approved (hub/src/issuestart.ts).
                approved=str(it.get("approved") or "") if author in bots else "")


class GitLabBoard:
    """Board = the GitLab board of the space's connected projects, read and written by the hub."""

    def __init__(self, space: str, agent: str, flow: Flow, bots: set[str], call: Callable[[dict], dict]):
        self.space, self.agent, self.flow = space, agent, flow
        self.bots = {b.lower() for b in bots}
        self.call = call

    def _frame(self, type_: str, **kw: Any) -> dict:
        return self.call({"type": type_, "space": self.space, "agent": self.agent, **kw})

    def _items(self, ref: str | None = None) -> list[Item]:
        r = self._frame("board.read", **({"ref": ref} if ref else {}))
        # The token's user writes the agents' notes and the issues created from briefling: trusted as a machine,
        # like the briefling App on GitHub (its words are never a human's go).
        bots = self.bots | ({str(r["me"]).lower()} if r.get("me") else set())
        return [parse_item(it, self.flow, bots) for it in r.get("items") or []]

    # ------------------------------------------------------------ read
    def items(self) -> list[Item]:
        return self._items()

    def item(self, ref: str) -> Item | None:
        split_ref(ref)
        found = self._items(ref)
        return found[0] if found else None

    # ------------------------------------------------------------ write
    def _write(self, ref: str, op: str, **kw: Any) -> None:
        split_ref(ref)
        self._frame("board.write", ref=ref, op=op, **kw)

    def move(self, ref: str, column: str) -> None:
        self._write(ref, "move", column=column)

    def set_field(self, ref: str, name: str, value: str) -> None:
        raise GitLabError("a GitLab board has no %r field: use labels (board_label)" % name)

    def labels(self, ref: str, add: Sequence[str] = (), remove: Sequence[str] = ()) -> None:
        if add or remove:
            self._write(ref, "labels", add=list(add), remove=list(remove))

    def comment(self, ref: str, body: str) -> None:
        self._write(ref, "comment", body=body)

    def upsert_status(self, ref: str, agent: str, body: str) -> None:
        if agent != self.agent:
            raise GitLabError("status of another agent")
        self._write(ref, "status", body=body)
