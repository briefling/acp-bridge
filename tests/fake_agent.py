"""A scripted ACP agent for the tests (newline-delimited JSON-RPC on stdio). Behaviour by keyword in the prompt:
PERMISSION (asks a permission, reports the answer), SLOW (waits for session/cancel), FS (asks the client to read a
file, reports the error code), DIE (exits mid-turn), WAIT (1.5 s after its tool call); otherwise echoes the prompt's last line in three chunks."""

import json
import sys
import threading
import time

sessions = {}  # sessionId -> prompts count
lock = threading.Lock()
cancelled = set()
loads = {}  # sessionId -> session/load count (shown in the echo when > 0)
replies = {}  # request id sent by the agent -> response
next_id = [1000]


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
            send({"id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                                        "agentInfo": {"name": "fake", "version": "1"}}})
        elif m == "session/new":
            sid = f"s{len(sessions) + 1}"
            sessions[sid] = 0
            send({"id": rid, "result": {"sessionId": sid}})
        elif m == "session/load":
            if p.get("sessionId") in sessions:
                loads[p["sessionId"]] = loads.get(p["sessionId"], 0) + 1
                send({"id": rid, "result": {}})
            else:
                send({"id": rid, "error": {"code": -32002, "message": "unknown session"}})
        elif m == "session/prompt":
            threading.Thread(target=prompt, args=(rid, p), daemon=True).start()
        elif m == "session/cancel":
            cancelled.add(p.get("sessionId"))


main()
