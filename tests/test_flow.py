"""The bridge's flow host (the plugin's flowrunner.py, by symlink) with the scripted agent: the hub pilots the agent by
default, the flow reads the (GitLab, through the hub) board, places the task, the agent works it with board_status
over the MCP server like a real agent, its stopReason ends the turn and the tick at once closes the task (flow.done)."""
import asyncio
import contextlib
import json

from test_bridge import running  # noqa: F401 (fake hub + bridge)

from briefling_acp import tools

SPACE, AGENT, P = "sandbox", "kiro", "acme/sub/app"
CONFIG = """departments:
  sandbox:
    flow: "dev"
    board:
      type: "gitlab"
    repos:
      - "acme/sub/app"
    staff:
      "kiro":
        - "dev"
agents:
  "kiro": {}
%s"""


def item(n, column="Ready"):
    return {"repo": P, "number": n, "title": "Fix %d" % n, "url": "u", "column": column, "labels": ["dev:kiro"],
            "author": "lead", "trusted": True, "created": "2026-11-22T09:00:00Z", "notes": [], "mrs": []}


class Board:
    """The hub's side of the flow: board.read / board.write on a fake GitLab board, flow.task / flow.done."""

    def __init__(self, hub, items):
        self.hub, self.items, self.frames, self.notes = hub, items, [], 0

    async def serve(self):
        while True:
            f = await self.hub.got.get()
            self.frames.append(f)
            cid, kind = f.get("client_id"), f.get("type")
            if kind == "board.read":
                its = [i for i in self.items if not f.get("ref") or "%s#%d" % (i["repo"], i["number"]) == f["ref"]]
                await self.hub.push({"type": "board.items", "client_id": cid, "me": "bot", "items": its})
            elif kind == "board.write":
                it = next(i for i in self.items if "%s#%d" % (i["repo"], i["number"]) == f["ref"])
                if f["op"] == "status":   # one status note, edited in place
                    self.notes += 1
                    it["notes"] = [n for n in it["notes"] if "uns:status" not in n["body"]] + [
                        {"id": self.notes, "author": "bot", "trusted": True, "body": f["body"], "created": "2026-11-23T10:00:00Z"}]
                await self.hub.push({"type": "board.written", "client_id": cid})
            elif kind == "flow.task":
                await self.hub.push({"type": "flow.result", "client_id": cid, "status": "started", "thread": "t%s" % f["ref"].split("#")[1]})
            elif kind == "flow.done":
                await self.hub.push({"type": "ack", "client_id": cid, "thread": "", "msg_id": ""})

    def of(self, kind):
        return [f for f in self.frames if f.get("type") == kind]


async def until(pred, timeout=15.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def flowing(tmp_path, items, limits=""):
    async with running(tmp_path, flow_dir=str(tmp_path / "flow")) as (hub, bridge):
        board = Board(hub, items)
        task = asyncio.create_task(board.serve())
        await hub.push({"type": "flow.config", "space": SPACE, "version": 1, "sha": "d" * 64,
                        "files": [{"path": "config.yml", "text": CONFIG % limits}]})
        await hub.push({"type": "flow.pilot", "space": SPACE, "agent": AGENT, "piloted": True})
        try:
            yield hub, bridge, board
        finally:
            task.cancel()


async def test_subscribe_says_the_bridge_flows_the_agents_with_no_unstrikeable_state(tmp_path, monkeypatch):
    monkeypatch.setenv("UNS_HOME", str(tmp_path / "uns"))
    async with running(tmp_path, flow_dir=str(tmp_path / "flow")) as (hub, _):
        assert hub.subscribe["bridge"]["flow_auto"] is True and hub.subscribe["bridge"]["uns"] == []


async def test_without_flow_dir_the_bridge_runs_no_flow(tmp_path):
    async with running(tmp_path) as (hub, bridge):
        assert bridge.flow is None and "flow_auto" not in hub.subscribe["bridge"]
        assert (await bridge.flow_tool("t1", "board_move", {"ref": P + "#1", "state": "doing"}))["error"] == "no_flow"


async def test_assigned_issue_becomes_a_task_turn_the_agent_closes_with_board_status(tmp_path, monkeypatch):
    monkeypatch.setenv("UNS_HOME", str(tmp_path / "uns"))
    async with flowing(tmp_path, [item(4)]) as (hub, bridge, board):
        await until(lambda: board.of("flow.done"))
        status = [f for f in board.frames if f.get("type") == "flow.config.status"]
        assert status and status[0]["ok"] is True and status[0]["agent"] == AGENT
        task = board.of("flow.task")[0]
        assert task["ref"] == P + "#4" and task["trigger"] == "dev.assigned" and task["agent"] == AGENT
        started = [f for f in board.frames if f.get("type") == "turn.started"]
        assert [f["thread"] for f in started] == ["t4"], "the task runs in the issue thread, no human message"
        writes = [(f["op"], f["body"].split("\n")[0]) for f in board.of("board.write")]
        assert [w[0] for w in writes] == ["status", "status"] and "working" in writes[0][1] and "done" in writes[1][1]
        reply = [f for f in board.frames if f.get("type") == "message.final" and f["thread"] == "t4"][-1]["text"]
        assert "board_status -> " in reply and "error=False" in reply, reply
        done = board.of("flow.done")[0]
        assert done["ref"] == P + "#4" and done["record"]["outcome"] == "done" and done["record"]["kind"] == "bug"
        assert done["record"]["cost"] is None, "ACP reports no cost"
        # the turn's end ticked at once: no new task for the same event, the host is free
        host = bridge.flow.hosts[SPACE]
        assert not host.task_turn and host.thread is None and len(board.of("flow.task")) == 1
        assert bridge.thread_space["t4"] == SPACE


async def test_blocked_task_closes_as_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("UNS_HOME", str(tmp_path / "uns"))
    it = item(5)
    it["title"] = "BLOCKME"
    async with flowing(tmp_path, [it]) as (_, _b, board):
        await until(lambda: board.of("flow.done"))
        assert board.of("flow.done")[0]["record"]["outcome"] == "blocked"


async def test_refused_push_blocks_the_task_with_the_cause_and_pauses_the_agent(tmp_path, monkeypatch):
    """Step 3: the first refused command of a task = board_status blocked (cause + config line), the agent paused,
    flow.state carries `perm` for the hub (Workflow); never the command line (it may hold a secret). Resume clears it."""
    monkeypatch.setenv("UNS_HOME", str(tmp_path / "uns"))
    it = item(8)
    it["title"] = "PUSHME"
    async with flowing(tmp_path, [it]) as (_, bridge, board):
        bridge.approvals_enabled = True
        await until(lambda: any((f["state"].get("perm") for f in board.of("flow.state"))))
        assert not board.of("ask"), "automatic tasks never wait for an interactive approval"
        note = [f["body"] for f in board.of("board.write") if f["op"] == "status"][-1]
        assert "waiting for a human" in note and "`git push`" in note and "s3cret" not in note, note
        assert "allow `git push`" in note, "fake agent: the generic line (no known agent)"
        state = [f["state"] for f in board.of("flow.state") if f["state"].get("perm")][-1]
        assert state["perm"]["what"] == "git push" and state["perm"]["ref"] == P + "#8"
        assert state["paused"]["by"] == "human" and "git push" in state["paused"]["reason"]
        assert any("git push" in a["text"] for a in state["alerts"])
        assert "s3cret" not in json.dumps(board.frames)
        host = bridge.flow.hosts[SPACE]
        await until(lambda: not host.task_turn)
        assert await bridge.flow.tick(host) == "paused" and len(board.of("flow.task")) == 1, "no next task meanwhile"
        assert not board.of("flow.done"), "the task closes once a human resumes (the engine reads its blocked status)"
        host.last_tick = 0
        await bridge.flow.on_pause({"space": SPACE, "agent": AGENT, "paused": False, "by": "Ada"})
        assert "perm" not in host.state and "perm" not in host.snapshot()
        await until(lambda: board.of("flow.done"))
        assert board.of("flow.done")[0]["record"]["outcome"] == "blocked"


async def test_tasks_per_day_cap_holds_with_no_cost(tmp_path, monkeypatch):
    """ACP gives no usage: the cap that holds is the config's tasks per day (max_events_per_day)."""
    monkeypatch.setenv("UNS_HOME", str(tmp_path / "uns"))
    async with flowing(tmp_path, [item(6), item(7)], "limits:\n  max_events_per_day: 1\n") as (_, bridge, board):
        await until(lambda: board.of("flow.done"))
        await bridge.flow.tick(bridge.flow.hosts[SPACE])
        assert [f["ref"] for f in board.of("flow.task")] == [P + "#6"]


async def test_board_tool_outside_a_flow_space_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("UNS_HOME", str(tmp_path / "uns"))
    async with flowing(tmp_path, []) as (hub, bridge, _):
        bridge.thread_space["t9"] = "other"
        out = await bridge.flow_tool("t9", "board_status", {"ref": P + "#1", "state": "working"})
        assert out["error"] == "no_flow" and "board this agent works" in out["detail"]
        bad = await bridge.flow_tool("t9", "flow_switch", {"link": "nope"})
        assert bad["ok"] is False and "whole link" in bad["detail"]


def test_board_tools_are_offered_with_the_plugins_schemas():
    from briefling_acp import tooldefs as td
    for name, schema in (("board_status", td.BOARD_STATUS_SCHEMA), ("board_move", td.BOARD_MOVE_SCHEMA),
                         ("board_comment", td.BOARD_COMMENT_SCHEMA), ("board_label", td.BOARD_LABEL_SCHEMA),
                         ("board_set", td.BOARD_SET_SCHEMA), ("flow_switch", td.FLOW_SWITCH_SCHEMA)):
        assert name in tools.NAMES and name in tools.FLOW_CALLS and schema in tools.SCHEMAS
    assert json.dumps(tools.mcp_tools())  # serialisable as tools/list
