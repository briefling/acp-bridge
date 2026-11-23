"""A scripted ACP agent for the tests (newline-delimited JSON-RPC on stdio). Behaviour by keyword in the prompt:
PERMISSION (asks a permission, reports the answer), SLOW (waits for session/cancel), FS (asks the client to read a
file, reports the error code), DIE (exits mid-turn), WAIT (1.5 s after its tool call), PERMTOOL (asks a permission for
a briefling tool), MCP (reports the MCP servers it got in session/new), BLOCKS (reports the attachment list and the
prompt's blocks after the text; FAKE_PROMPT_CAPS = the promptCapabilities it advertises), lines `SAY <text>` and `TOOL <name> <json args>`
(says the text, or calls the tool through the MCP server of session/new, like a real agent, and reports the result);
otherwise echoes the prompt's last line in three chunks."""

import os
import subprocess
import urllib.error
import urllib.request

import json
import sys
import threading
import time

HTTP = os.environ.get("FAKE_MCP_HTTP") == "1"  # advertise mcpCapabilities.http, like claude-agent-acp

sessions = {}  # sessionId -> prompts count
lock = threading.Lock()
cancelled = set()
loads = {}  # sessionId -> session/load count (shown in the echo when > 0)
replies = {}  # request id sent by the agent -> response
next_id = [1000]
mcp_conf = {}  # sessionId -> mcpServers of session/new|load
mcp_proc = {}  # sessionId -> (process, next id) of its first MCP server, started at the first tool call


def mcp_rpc(sid, method, params):
    """One request to the session's MCP server (newline-delimited JSON-RPC on its stdio, or one POST per message to
    an `http` server), as an agent does."""
    conf = mcp_conf[sid][0]
    if conf.get("type") == "http":
        return http_rpc(conf, method, params)
    if sid not in mcp_proc:
        conf = mcp_conf[sid][0]
        env = {**os.environ, **{e["name"]: e["value"] for e in conf.get("env", [])}}
        p = subprocess.Popen([conf["command"], *conf.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env=env, text=True)
        mcp_proc[sid] = [p, 0]
        mcp_proc[sid][1] += 1
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mcp_proc[sid][1], "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "1"}}}) + "\n")
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        json.loads(p.stdout.readline())
    p = mcp_proc[sid][0]
    mcp_proc[sid][1] += 1
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mcp_proc[sid][1], "method": method, "params": params}) + "\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())


def http_rpc(conf, method, params):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               **{h["name"]: h["value"] for h in conf.get("headers", [])}}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    url = conf["url"].replace("//host.docker.internal:", "//127.0.0.1:")  # what the sandbox's host proxy does
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no HTTP(S)_PROXY from the test env
    try:
        with opener.open(urllib.request.Request(url, body, headers), timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"result": {"content": [{"text": json.dumps({"http": e.code})}], "isError": True}}


def tool_lines(sid, text):
    """SAY / TOOL lines, in order; True when there was at least one."""
    did = False
    for line in text.splitlines():
        if line.startswith("SAY "):
            chunk(sid, line[4:] + "\n")
            did = True
        elif line.startswith("TOOL "):
            name, _, raw = line[5:].partition(" ")
            update(sid, {"sessionUpdate": "tool_call", "toolCallId": f"t{name}", "title": f"@briefling/{name}",
                         "kind": "other", "status": "in_progress"})
            if name == "LIST":
                r = mcp_rpc(sid, "tools/list", {})
                chunk(sid, "tools: " + ",".join(t["name"] for t in r["result"]["tools"]) + "\n")
            else:
                r = mcp_rpc(sid, "tools/call", {"name": name, "arguments": json.loads(raw or "{}")})
                res = r.get("result") or {}
                chunk(sid, f"{name} -> {res.get('content', [{}])[0].get('text')} error={res.get('isError')}\n")
                more = [{"type": c.get("type"), "mimeType": c.get("mimeType"), "n": len(c.get("data") or "")}
                        for c in res.get("content", [])[1:]]
                if more:  # MCP content after the first block (attachment_get's image)
                    chunk(sid, f"{name} blocks: {json.dumps(more)}\n")
            did = True
    return did


def send(obj):
    with lock:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **obj}) + "\n")
        sys.stdout.flush()


def update(sid, u):
    send({"method": "session/update", "params": {"sessionId": sid, "update": u}})


def chunk(sid, text):
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


def ask(method, params):
    next_id[0] += 1
    rid = next_id[0]
    send({"id": rid, "method": method, "params": params})
    for _ in range(500):
        if rid in replies:
            return replies.pop(rid)
        time.sleep(0.01)
    return None


def prompt(rid, p):
    sid = p["sessionId"]
    sessions[sid] += 1
    text = p["prompt"][0]["text"]
    if "DIE" in text:
        chunk(sid, "about to die")
        sys.stdout.flush()
        time.sleep(0.2)
        import os
        os._exit(3)
    if "PERMISSION" in text:
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Run rm -rf build", "kind": "execute", "status": "pending"})
        r = ask("session/request_permission", {"sessionId": sid, "toolCall": {"toolCallId": "c1", "title": "Run rm -rf build", "status": "pending"},
                                               "options": [{"optionId": "ok", "name": "Allow", "kind": "allow_once"},
                                                           {"optionId": "okk", "name": "Always", "kind": "allow_always"},
                                                           {"optionId": "no", "name": "Deny", "kind": "reject_once"}]})
        chunk(sid, "permission answer: " + json.dumps(r.get("result") if r else None, sort_keys=True))
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "FS" in text:
        r = ask("fs/read_text_file", {"sessionId": sid, "path": "/etc/passwd"})
        chunk(sid, "fs answer: " + str((r or {}).get("error", {}).get("code")))
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "SLOW" in text:
        chunk(sid, "working")
        for _ in range(1000):
            if sid in cancelled:
                cancelled.discard(sid)
                send({"id": rid, "result": {"stopReason": "cancelled"}})
                return
            time.sleep(0.01)
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "MCP" in text:
        chunk(sid, "mcp: " + json.dumps([{**s, "env": sorted(e["name"] for e in s.get("env", []))} if "env" in s else s
                                          for s in mcp_conf.get(sid, [])]))
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "BLOCKS" in text:  # what came with the prompt: the attachment list, then each block after the text
        listed = [l for l in text.splitlines() if l.startswith("- id ")]
        blocks = [{"type": b.get("type"), "mimeType": b.get("mimeType") or (b.get("resource") or {}).get("mimeType"),
                   "n": len(b.get("data") or ""), "uri": (b.get("resource") or {}).get("uri"),
                   "text": (b.get("resource") or {}).get("text")} for b in p["prompt"][1:]]
        chunk(sid, "blocks: " + json.dumps({"listed": listed, "blocks": blocks}))
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "PERMTOOL" in text:
        r = ask("session/request_permission", {"sessionId": sid, "toolCall": {"toolCallId": "c2", "title": "@briefling/turn_outcome"},
                                               "options": [{"optionId": "no", "name": "Deny", "kind": "reject_once"}]})
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "board task started by the flow" in text and "PUSHME" in text:  # a flow task whose git push is refused
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c3", "title": "git push", "kind": "execute", "status": "pending"})
        r = ask("session/request_permission", {"sessionId": sid, "toolCall": {"toolCallId": "c3", "title": "`TOKEN=s3cret git push origin fix-1`",
                "kind": "execute", "rawInput": {"command": "TOKEN=s3cret git push origin fix-1"}},
                "options": [{"optionId": "no", "name": "Deny", "kind": "reject_once"}]})
        chunk(sid, "push refused, stopping")
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "board task started by the flow" in text:  # a flow task: works the item through the board tools, as told
        import re
        ref = re.search(r"\[flow\] agent=\S+ department=\S+ event=\S+ item=(\S+)", text).group(1)
        outcome = "blocked" if "BLOCKME" in text else "done"
        tool_lines(sid, "TOOL board_status " + json.dumps({"ref": ref, "state": "working", "todo": "fix"}) + "\n"
                   "TOOL board_status " + json.dumps({"ref": ref, "state": outcome, "learned": "none", "kind": "bug",
                                                      "note": "need a human"}))
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if tool_lines(sid, text):
        send({"id": rid, "result": {"stopReason": "end_turn"}})
        return
    if "SILENT" in text:
        send({"id": rid, "result": {"stopReason": "refusal"}})
        return
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "c0", "title": "Reading README.md", "kind": "read", "status": "in_progress"})
    if "WAIT" in text:  # long enough for the bridge to relay the tool call as a status
        time.sleep(1.5)
    last = text.strip().splitlines()[-1]
    tag = f"{sid}#{sessions[sid]}" + (f" loaded {loads[sid]}" if loads.get(sid) else "")
    for part in (f"Echo[{tag}]", ": ", last):
        chunk(sid, part)
        time.sleep(0.05)
    send({"id": rid, "result": {"stopReason": "end_turn"}})


def main():
    for line in sys.stdin:
        msg = json.loads(line)
        m, rid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if m is None:
            replies[rid] = msg
        elif m == "initialize":
            caps = {"loadSession": True, **({"mcpCapabilities": {"http": True}} if HTTP else {})}
            if os.environ.get("FAKE_PROMPT_CAPS"):  # e.g. {"image": true, "embeddedContext": true}
                caps["promptCapabilities"] = json.loads(os.environ["FAKE_PROMPT_CAPS"])
            send({"id": rid, "result": {"protocolVersion": 1, "agentCapabilities": caps,
                                        "agentInfo": {"name": "fake", "version": "1"}}})
        elif m == "session/new":
            sid = f"s{len(sessions) + 1}"
            sessions[sid] = 0
            mcp_conf[sid] = p.get("mcpServers") or []
            send({"id": rid, "result": {"sessionId": sid}})
        elif m == "session/load":
            if p.get("sessionId") in sessions:
                loads[p["sessionId"]] = loads.get(p["sessionId"], 0) + 1
                mcp_conf[p["sessionId"]] = p.get("mcpServers") or []
                mcp_proc.pop(p["sessionId"], None)
                send({"id": rid, "result": {}})
            else:
                send({"id": rid, "error": {"code": -32002, "message": "unknown session"}})
        elif m == "session/prompt":
            threading.Thread(target=prompt, args=(rid, p), daemon=True).start()
        elif m == "session/cancel":
            cancelled.add(p.get("sessionId"))


main()
