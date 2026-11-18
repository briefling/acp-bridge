"""Self-update of this bridge, offered by the hub (`subscribed.release` at each connection, `release` after a deploy).

Same gates as the Hermes plugin's self-update: two sources must agree before any new code lands, the release the
deployed hub announces (version + commit of the public repo) and the public repo's own tag `v<version>` (`git
ls-remote`). Then the same installer that installed this bridge (pip: pipx or a venv; uv: `uv tool install` or a uv
venv) installs that exact commit into this very environment, a fresh interpreter checks it imports, reports that
version and was installed from that commit, and the bridge restarts itself (same arguments) once no turn runs.
Any failure: the previous commit is installed back, nothing restarts, the commit is recorded as failed and never
retried (a newer release is). Never a downgrade.

Not updated by itself (the hub shows why to the space's leads when the bridge is behind): `off`
(BRIEFLING_ACP_AUTO_UPDATE=0), `pinned` (run by `uvx --from …@<tag>`: the launcher picks the version), `manual` (a
checkout, an editable install, or no installer at hand), `failed` (the last attempt failed).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

DIST = "briefling-acp-bridge"
REPO_URL = "https://github.com/briefling/acp-bridge"
VERSION_RE = re.compile(r"^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OFF_ENV = "BRIEFLING_ACP_AUTO_UPDATE"
FROM_ENV = "BRIEFLING_ACP_UPDATED_FROM"  # set for the restarted process: the version it was updated from
CMD_TIMEOUT_S = 300

# Run in a fresh interpreter of this environment: what is installed now (version, commit it came from).
_CHECK = ("import json, importlib.metadata as m, briefling_acp as b; "
          "d = json.loads(m.distribution('" + DIST + "').read_text('direct_url.json') or '{}'); "
          "print(json.dumps({'version': b.__version__, 'commit': (d.get('vcs_info') or {}).get('commit_id', '')}))")

Run = Callable[..., subprocess.CompletedProcess]


def _run(argv: Sequence[str], timeout: int = CMD_TIMEOUT_S) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PIP_USER": "0"}
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)


def parse_version(v: Any) -> Optional[tuple]:
    m = VERSION_RE.match(v) if isinstance(v, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(release: str, current: str) -> bool:
    a, b = parse_version(release), parse_version(current)
    return a is not None and b is not None and a > b


def valid_release(release: Any) -> Optional[Dict[str, str]]:
    """The hub's `{version, commit}`, or None when malformed (never a reason to act)."""
    if not isinstance(release, dict):
        return None
    v, c = release.get("version"), release.get("commit")
    if parse_version(v) is None or not (isinstance(c, str) and SHA_RE.match(c)):
        return None
    return {"version": v, "commit": c}


def enabled(value: Optional[str]) -> bool:
    return (value or "1").strip().lower() not in ("0", "false", "no", "off")


class Install:
    """How this bridge was installed, read from its own package metadata (PEP 610 `direct_url.json`, `INSTALLER`)."""

    def __init__(self, installer: str, commit: str, url: str, editable: bool, prefix: str, uv_cache: str = ""):
        self.installer, self.commit, self.url, self.editable = installer, commit, url, editable
        self.prefix, self.uv_cache = prefix, uv_cache

    @classmethod
    def here(cls) -> "Install":
        import importlib.metadata as md
        try:
            d = md.distribution(DIST)
        except md.PackageNotFoundError:
            return cls("", "", "", False, sys.prefix)
        try:
            du = json.loads(d.read_text("direct_url.json") or "{}")
        except ValueError:
            du = {}
        installer = (d.read_text("INSTALLER") or "").strip().lower()
        uv_cache = ""
        if installer == "uv" and shutil.which("uv"):
            try:
                uv_cache = _run(["uv", "cache", "dir"], timeout=20).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        return cls(installer, str((du.get("vcs_info") or {}).get("commit_id") or ""), str(du.get("url") or ""),
                   bool((du.get("dir_info") or {}).get("editable")), sys.prefix, uv_cache)

    @property
    def from_public_repo(self) -> bool:
        return self.url.rstrip("/").removesuffix(".git") == REPO_URL and bool(SHA_RE.match(self.commit))

    @property
    def ephemeral(self) -> bool:
        """An `uvx` environment: it lives in uv's cache and the launcher's `--from` decides what runs."""
        return bool(self.uv_cache) and os.path.realpath(self.prefix).startswith(os.path.realpath(self.uv_cache) + os.sep)

    def install_argv(self, commit: str) -> Optional[List[str]]:
        spec = f"git+{REPO_URL}@{commit}"
        if self.installer == "pip":
            return [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", spec]
        if self.installer == "uv" and shutil.which("uv"):
            return ["uv", "pip", "install", "-q", "--python", sys.executable, spec]
        return None


def blocked(install: Install, on: bool, current: str, state: Dict[str, Any]) -> Optional[str]:
    """Why this bridge will not install newer releases by itself (sent at subscribe), None when it will."""
    if not on:
        return "off"
    if install.ephemeral:
        return "pinned"
    if install.editable or not install.from_public_repo or install.install_argv(install.commit) is None:
        return "manual"
    if is_newer(str(state.get("version") or ""), current):
        return "failed"
    return None


def read_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, **state: Any) -> None:
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def tag_commit(version: str, run: Run = _run) -> str:
    """The commit the public repo's tag v<version> points to ('' when absent or unreadable)."""
    try:
        p = run(["git", "ls-remote", REPO_URL, f"refs/tags/v{version}", f"refs/tags/v{version}^{{}}"], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    refs = dict(reversed(line.split("\t", 1)) for line in p.stdout.splitlines() if "\t" in line) if p.returncode == 0 else {}
    return refs.get(f"refs/tags/v{version}^{{}}") or refs.get(f"refs/tags/v{version}") or ""


def installed(run: Run = _run) -> Dict[str, str]:
    try:
        p = run([sys.executable, "-c", _CHECK], timeout=60)
        out = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else {}
        return out if isinstance(out, dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {}


def apply(install: Install, release: Dict[str, str], current: str, state_path: str, *, run: Run = _run) -> bool:
    """Installs the release if every gate passes; True = installed and checked, restart to run it."""
    version, commit = release["version"], release["commit"]
    if not is_newer(version, current) or blocked(install, True, current, {}):
        return False
    if read_state(state_path).get("commit") == commit:
        return False  # this exact commit already failed here: never retried
    public = tag_commit(version, run)
    if public != commit:
        # A lagging or rewritten tag, or a hub announcing something the public repo does not say: wait, no record.
        logger.warning("self-update to %s skipped: the public tag v%s is %s, the hub says %s", version, version,
                       public[:12] or "absent", commit[:12])
        return False
    logger.info("self-update: installing bridge %s (%s)", version, commit[:12])
    try:
        p = run(install.install_argv(commit))
        ok = p.returncode == 0
        why = (p.stderr or p.stdout or "").strip()[-300:]
    except (OSError, subprocess.SubprocessError) as e:
        ok, why = False, type(e).__name__
    now = installed(run) if ok else {}
    if ok and now == {"version": version, "commit": commit}:
        logger.info("self-update: bridge %s installed, restart when no turn runs", version)
        return True
    logger.error("self-update to %s failed (%s): putting %s back", version, why or f"installed {now}", install.commit[:12])
    try:
        back = run(install.install_argv(install.commit)).returncode == 0 and installed(run).get("commit") == install.commit
    except (OSError, subprocess.SubprocessError):
        back = False
    if not back:
        logger.error("self-update: could not put %s back: reinstall the bridge by hand", install.commit[:12])
    save_state(state_path, version=version, commit=commit)
    return False


def restart_argv(args: Sequence[str]) -> List[str]:
    """The same bridge, same arguments, in the freshly installed code of this environment."""
    return [sys.executable, "-m", "briefling_acp", *args]
