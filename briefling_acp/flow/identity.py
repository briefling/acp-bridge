"""GitHub identity of an agent: installation token of its GitHub App, or None for the machine's `gh` auth.

The JWT is signed by the `openssl` CLI (no crypto reimplemented here); the installation token is cached 50 min.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


class IdentityError(Exception):
    pass


def default_cache_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "briefling" / "tokens"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def app_token(app: dict, owner: str, cache_dir: Path | str | None = None) -> str:
    """Installation token of a GitHub App (JWT signed with openssl), cached 50 min.
    app: {app_id, app_key: path to the PEM, installation_id (optional, else looked up on the org `owner`)}."""
    cache = Path(cache_dir or default_cache_dir()) / ("token-%s.json" % app["app_id"])
    if cache.exists():
        c = json.loads(cache.read_text())
        if c["exp"] > time.time() + 300:
            return c["token"]
    now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app["app_id"])}).encode())
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", os.path.expanduser(app["app_key"])],
                         input=("%s.%s" % (head, body)).encode(), capture_output=True)
    if sig.returncode != 0:
        raise IdentityError("openssl sign failed: " + sig.stderr.decode()[:300])
    jwt = "%s.%s.%s" % (head, body, _b64(sig.stdout))

    def call(method: str, url: str) -> dict:
        req = urllib.request.Request(url, method=method, headers={
            "Authorization": "Bearer " + jwt, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "briefling-flow"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    try:
        inst = app.get("installation_id") or call("GET", "https://api.github.com/orgs/%s/installation" % owner)["id"]
        tok = call("POST", "https://api.github.com/app/installations/%s/access_tokens" % inst)
    except urllib.error.HTTPError as e:
        raise IdentityError("GitHub App %s: HTTP %d (is the App installed on %s?)" % (app["app_id"], e.code, owner)) from e
    cache.parent.mkdir(parents=True, exist_ok=True)
    exp = time.mktime(time.strptime(tok["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    cache.write_text(json.dumps({"token": tok["token"], "exp": exp}))
    os.chmod(cache, 0o600)
    return tok["token"]


def make_token(identity: dict | None, owner: str, cache_dir: Path | str | None = None) -> str | None:
    """Token for GH_TOKEN: the agent's App ({app_id, app_key, [installation_id]}, the keys of an unstrikeable
    local.yml agents entry), or None = use the machine's `gh` auth (empty identity, or no app_key)."""
    if not identity or not identity.get("app_key"):
        return None
    return app_token(identity, owner, cache_dir)
