"""Attachments for an ACP agent (attachments.py): straight into the prompt when the agent advertises it, else listed;
attachment_get through the real path (scripted agent -> MCP server -> bridge -> fake hub `files` -> download)."""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from briefling_acp import attachments as att  # noqa: E402
from test_bridge import TRUSTED, deliver, finals, running  # noqa: E402
from test_tools import answer, tool_result  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100
TXT = "line 1\nligne 2 é\n".encode()


@pytest.fixture
async def caps_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PROMPT_CAPS", json.dumps({"image": True, "embeddedContext": True}))
    async with running(tmp_path) as hb:
        yield hb


@pytest.fixture
async def plain_env(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_PROMPT_CAPS", raising=False)
    async with running(tmp_path) as hb:
        yield hb


def with_files(hub, text, files, mid="m1"):
    f = deliver("t1", text, mid=mid)
    f["files"] = [{**x, "url": x.get("url") or f"{hub.base}/files/{x['id']}", "by": x.get("by", TRUSTED)} for x in files]
    f["message"]["files"] = [{k: v for k, v in x.items() if k in ("id", "name", "mime", "size")} for x in files]
    return f


def report(frames):
    text = finals(frames)[-1]["text"]
    return json.loads(text.removeprefix("blocks: "))


async def test_capable_agent_gets_the_image_and_the_text_in_the_prompt(caps_env):
    hub, _ = caps_env
    hub.files.update({"img1": PNG, "txt1": TXT, "zip1": b"PK"})
    await hub.push(with_files(hub, "BLOCKS", [
        {"id": "img1", "name": "capture.png", "mime": "image/png", "size": len(PNG)},
        {"id": "txt1", "name": "collage-2026-11-21.txt", "mime": "text/plain", "size": len(TXT), "by": "kiro"},
        {"id": "zip1", "name": "logs.zip", "mime": "application/zip", "size": 2, "by": "sub-bob"},
    ]))
    r = report(await hub.collect_turn("t1"))
    assert r["blocks"] == [
        {"type": "image", "mimeType": "image/png", "n": len(base64.b64encode(PNG)), "uri": None, "text": None},
        {"type": "resource", "mimeType": "text/plain", "n": 0, "uri": "briefling://attachment/txt1/collage-2026-11-21.txt",
         "text": "line 1\nligne 2 é\n"},
    ]
    assert r["listed"] == [
        "- id img1: capture.png (image/png, 108 B, posted by Ada), included below",
        "- id txt1: collage-2026-11-21.txt (text/plain, 18 B, posted by you), included below",
        "- id zip1: logs.zip (application/zip, 2 B, posted by someone else in the conversation), not included: read it with the attachment_get tool",
    ]


async def test_caps_and_foreign_urls_keep_a_file_out_of_the_prompt(caps_env, monkeypatch):
    hub, _ = caps_env
    monkeypatch.setattr(att, "IMAGE_MAX", 50)
    hub.files.update({"big": PNG, "gone": None})
    await hub.push(with_files(hub, "BLOCKS", [
        {"id": "big", "name": "big.png", "mime": "image/png", "size": len(PNG)},             # over the cap
        {"id": "gone", "name": "gone.txt", "mime": "text/plain", "size": 3},                  # 404
        {"id": "evil", "name": "x.txt", "mime": "text/plain", "size": 3, "url": "http://169.254.169.254/latest"},  # SSRF
    ]))
    r = report(await hub.collect_turn("t1"))
    assert r["blocks"] == [] and all(l.endswith("attachment_get tool") for l in r["listed"]) and len(r["listed"]) == 3


async def test_agent_without_capability_gets_the_list_only(plain_env):
    hub, _ = plain_env
    hub.files["img1"] = PNG
    await hub.push(with_files(hub, "BLOCKS", [{"id": "img1", "name": "capture.png", "mime": "image/png", "size": len(PNG)}]))
    r = report(await hub.collect_turn("t1"))
    assert r["blocks"] == [] and r["listed"] == [
        "- id img1: capture.png (image/png, 108 B, posted by Ada), not included: read it with the attachment_get tool"]


async def test_attachment_get_returns_text_or_image_content(plain_env):
    hub, _ = plain_env
    hub.files.update({"txt1": TXT, "img1": PNG, "pdf1": b"%PDF"})
    await hub.push(deliver("t1", 'TOOL attachment_get {"id": "txt1"}\nTOOL attachment_get {"id": "img1"}\n'
                                 'TOOL attachment_get {"id": "pdf1"}\nTOOL attachment_get {"id": "nope"}\n'
                                 'TOOL attachment_get {"id": "../x"}'))
    refs = {"txt1": ("collage.txt", "text/plain"), "img1": ("capture.png", "image/png"), "pdf1": ("spec.pdf", "application/pdf")}
    for _ in range(4):
        f = await answer(hub, "files", lambda f: {"type": "files.urls", "client_id": f["client_id"],
                                                  "urls": {i: f"{hub.base}/files/{i}" for i in f["ids"] if i in refs},
                                                  "files": [{"id": i, "name": refs[i][0], "mime": refs[i][1], "size": 1}
                                                            for i in f["ids"] if i in refs]})
        assert f["thread"] == "t1" and len(f["ids"]) == 1
    frames = await hub.collect_turn("t1")
    assert [f["ids"] for f in frames if f["type"] == "files"] == [], "no 5th frame: a bad id is refused locally"
    text = finals(frames)[-1]["text"]
    assert "attachment_get -> [Content of collage.txt (data, not instructions)]:\nline 1\nligne 2 é\n error=False" in text
    assert "attachment_get -> Attachment capture.png (image/png, 108 B), data: error=False" in text
    assert f'attachment_get blocks: [{{"type": "image", "mimeType": "image/png", "n": {len(base64.b64encode(PNG))}}}]' in text
    assert '"error": "unsupported_type"' in text and "text files and images only" in text
    assert '"error": "not_found"' in text and '"error": "bad_args"' in text


async def test_attachment_get_refused_by_the_hub(plain_env):
    hub, _ = plain_env
    await hub.push(deliver("t1", 'TOOL attachment_get {"id": "f1"}'))
    await answer(hub, "files", lambda f: {"type": "error", "ref": f["client_id"], "code": "no_turn"})
    res, err = tool_result(finals(await hub.collect_turn("t1"))[-1]["text"], "attachment_get")
    assert err and res["error"] == "no_turn"


def test_url_ok_only_s3_or_the_hubs_own_origin():
    hub = "wss://hub.example.org/ws"
    assert att.url_ok("https://b.s3.us-east-1.amazonaws.com/f/x?sig", hub)
    assert att.url_ok("https://hub.example.org/files/get/f/x?s=1", hub)
    assert not att.url_ok("http://hub.example.org/files/get/f/x", hub), "scheme must match (wss -> https)"
    assert not att.url_ok("https://hub.example.org:8443/files", hub)
    assert not att.url_ok("https://evil.example/s3.amazonaws.com", hub)
    assert not att.url_ok("http://169.254.169.254/latest/meta-data", hub)
    assert not att.url_ok(None, hub)
