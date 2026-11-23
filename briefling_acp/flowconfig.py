"""Flow config pushed by the hub (docs/design-flow.md § 4.2): the bundle edited in briefling, written to disk and
loaded with the engine's ``load_company`` before it replaces the one in use.

The hub sends ``flow.config {space, version, sha, files}``; this module checks the bundle (closed list of paths, size,
no credential), writes it in a fresh folder, loads it, and only then makes it current. A bundle that fails keeps the
last good one in use, and the error goes back to the hub (``flow.config.status``) for the editor to show.

Same rules as protocol/flowteam.ts (BUNDLE_PATH_RE, SECRET_RE, MAX_BUNDLE_BYTES, MAX_BUNDLE_FILES): the hub checks
them before sending, the plugin checks them again since it is the one that writes to disk.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .flow.config import load_company

BUNDLE_PATH_RE = re.compile(
    r"^(?:config\.yml|culture\.md|agents/[a-z][a-z0-9_-]{0,31}\.md|flows/[a-z0-9][a-z0-9_-]{0,39}\.yml"
    r"|playbooks(?:/[a-z0-9][a-z0-9_-]{0,63}){1,3}\.md)$")
SECRET_RE = re.compile(r"-----BEGIN|app_key|(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BUNDLE_BYTES = 64 * 1024
MAX_BUNDLE_FILES = 200
ERROR_MAX = 500


class BundleError(Exception):
    """The bundle itself is refused (shape, path, size, credential): nothing was written."""


def check_bundle(files: Any) -> List[Tuple[str, str]]:
    """(path, text) pairs of a valid bundle, else BundleError naming the first problem."""
    if not isinstance(files, list) or not files:
        raise BundleError("no files")
    if len(files) > MAX_BUNDLE_FILES:
        raise BundleError("too many files (%d > %d)" % (len(files), MAX_BUNDLE_FILES))
    out, seen, size = [], set(), 0
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("text"), str):
            raise BundleError("malformed file entry")
        path, text = f["path"], f["text"]
        if not BUNDLE_PATH_RE.match(path):
            raise BundleError("path not allowed: %r" % path[:120])
        if path in seen:
            raise BundleError("duplicate path: %s" % path)
        if SECRET_RE.search(text):
            raise BundleError("%s looks like it contains a key or a token" % path)
        seen.add(path)
        size += len((path + text).encode("utf-8"))
        out.append((path, text))
    if size > MAX_BUNDLE_BYTES:
        raise BundleError("bundle too big (%d bytes > %d)" % (size, MAX_BUNDLE_BYTES))
    if "config.yml" not in seen:
        raise BundleError("config.yml missing")
    return out


class ConfigStore:
    """The hub's bundles of one space: ``<root>/<sha>/`` folders, ``current.json`` = the good one in use."""

    def __init__(self, root: Path):
        self.root = root

    def current(self) -> Optional[Dict[str, Any]]:
        """{version, sha, dir} of the bundle in use, None before the first good one."""
        try:
            c = json.loads((self.root / "current.json").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(c, dict) or not SHA_RE.match(str(c.get("sha") or "")) or not (self.root / c["sha"]).is_dir():
            return None
        return {"version": c.get("version"), "sha": c["sha"], "dir": self.root / c["sha"]}

    def apply(self, version: int, sha: str, files: Any) -> Tuple[bool, Optional[str]]:
        """Write, load, switch. (True, None) = in use (or already was); (False, error) = refused, previous kept."""
        if not SHA_RE.match(str(sha)):
            return False, "bad sha"
        cur = self.current()
        if cur and cur["sha"] == sha:
            if cur["version"] != version:
                self._point(version, sha)
            return True, None
        tmp = self.root / (".tmp-%s" % sha)
        try:
            pairs = check_bundle(files)
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)
            for path, text in pairs:
                p = tmp / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(text.encode("utf-8"))   # bytes: no newline translation
            load_company(tmp)
        except Exception as e:  # BundleError, ConfigError, YAML errors, anything the engine raises on a bad config
            shutil.rmtree(tmp, ignore_errors=True)
            msg = str(e).replace(str(tmp) + os.sep, "").replace(str(tmp), ".") or type(e).__name__
            return False, msg[:ERROR_MAX]
        final = self.root / sha
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmp, final)
        prev = cur["sha"] if cur else None
        self._point(version, sha)
        self._prune(keep={sha, prev})
        return True, None

    def _point(self, version: int, sha: str) -> None:
        t = self.root / "current.json.tmp"
        t.write_text(json.dumps({"version": version, "sha": sha}))
        os.replace(t, self.root / "current.json")

    def _prune(self, keep: set) -> None:
        """The current bundle and the previous one stay (a tick may still be reading the previous one)."""
        for d in self.root.iterdir():
            if d.is_dir() and d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)
