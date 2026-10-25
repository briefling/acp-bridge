"""`hermes briefling join <code-or-link>`: redeem an agent invitation and write the credentials into the
active profile's .env.

The invitation link is `<AppUrl>/agent.md?code=<code>`. The command reads `<AppUrl>/config.json` for the hub
WebSocket URL, opens a join connection (subprotocols `briefling.v2`, `join.<code>`), sends `agent.join` and waits
for `agent.joined`, which carries the instance's Cognito credentials. The client secret is written to the .env only:
never printed, logged or put in an exception message (an agent may run this command, its output enters its context).
The gateway is not restarted here: the operator does it (their approval gates it).
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp

# Same values as adapter.PROTOCOL / adapter.PROTOCOL_VERSION (pinned by tests/test_join.py). Kept here so this module
# imports without Hermes: the ACP bridge (acp-bridge/, outside Hermes) reuses it as is.
PROTOCOL = "briefling.v2"
PROTOCOL_VERSION = 2

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")  # AGENT_CODE_RE in protocol/frames.ts
TIMEOUT_S = 20
LOCAL_HOSTS = {"127.0.0.1", "localhost"}

HUB_ERRORS = {
    "invite_used": "this code was already used: ask the admin for a new link",
    "invite_expired": "this code has expired: ask the admin for a new link",
    "invite_revoked": "this invitation was revoked: ask the admin for a new link",
    "agent_exists": "this agent is already connected to briefling by another instance: ask the admin",
    "forbidden": "the hub refused this code: ask the admin for a new link",
}


class JoinError(Exception):
    """Failure shown to the operator as is. Never build one from a value that may hold the secret."""


def _check_url(url: str, schemes: Tuple[str, str]) -> str:
    """`schemes` = (secure, plain): plain is accepted for a local test server only."""
    u = urlparse(url)
    if u.scheme == schemes[0] or (u.scheme == schemes[1] and u.hostname in LOCAL_HOSTS):
        if u.hostname:
            return url
    raise JoinError(f"{url!r} is not a {schemes[0]}:// URL")


def parse_target(target: str, app: Optional[str]) -> Tuple[str, str]:
    """Return (app URL without trailing slash, code) from a bare code + --app, or from the invitation link."""
    target = target.strip()
    if "://" in target:
        u = urlparse(target)
        code = (parse_qs(u.query).get("code") or [""])[0]
        if not app:
            app = f"{u.scheme}://{u.netloc}{u.path.rsplit('/', 1)[0]}"
    else:
        code = target
        if not app:
            raise JoinError("a bare code needs --app <AppUrl> (or paste the full invitation link)")
    if not CODE_RE.match(code):
        raise JoinError("this is not a briefling invitation code (32 characters expected)")
    return _check_url(app.rstrip("/"), ("https", "http")), code


def _joined_ok(frame: Dict[str, Any]) -> bool:
    c = frame.get("credentials")
    return (isinstance(frame.get("agent"), str) and isinstance(frame.get("spaces"), list)
            and isinstance(frame.get("allowed"), list) and isinstance(c, dict)
            and all(isinstance(c.get(k), str) and c.get(k) for k in ("client_id", "client_secret", "token_url")))


async def redeem(app: str, code: str, timeout: float = TIMEOUT_S) -> Tuple[str, Dict[str, Any]]:
    """Return (wsUrl, agent.joined frame). Raises JoinError on any refusal; the socket is always closed."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        try:
            async with session.get(f"{app}/config.json") as r:
                if r.status != 200:
                    raise JoinError(f"cannot read {app}/config.json (HTTP {r.status})")
                config = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise JoinError(f"cannot read {app}/config.json ({type(e).__name__})") from None
        ws_url = config.get("wsUrl") if isinstance(config, dict) else None
        if not isinstance(ws_url, str):
            raise JoinError(f"{app}/config.json has no wsUrl")
        _check_url(ws_url, ("wss", "ws"))

        try:
            ws = await session.ws_connect(ws_url, protocols=(PROTOCOL, f"join.{code}"), heartbeat=None)
        except aiohttp.WSServerHandshakeError as e:
            if e.status in (401, 403):
                raise JoinError("invalid code: check the link, or ask the admin for a new one") from None
            raise JoinError(f"the hub refused the connection (HTTP {e.status})") from None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise JoinError(f"cannot reach the hub at {ws_url} ({type(e).__name__})") from None

        client_id = secrets.token_urlsafe(12)
        async with ws:
            await ws.send_str(json.dumps({"v": PROTOCOL_VERSION, "type": "agent.join", "client_id": client_id}))
            try:
                async with asyncio.timeout(timeout):
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            frame = json.loads(msg.data)
                        except ValueError:
                            continue
                        if not isinstance(frame, dict):
                            continue
                        if frame.get("type") == "agent.joined" and frame.get("client_id") == client_id:
                            if not _joined_ok(frame):
                                raise JoinError("the hub answered a malformed agent.joined frame")
                            return ws_url, frame
                        if frame.get("type") == "error" and frame.get("ref") in (None, client_id):
                            code_ = str(frame.get("code"))
                            raise JoinError(HUB_ERRORS.get(code_, f"the hub refused the invitation ({code_})"))
            except TimeoutError:
                raise JoinError(f"no answer from the hub within {timeout:g} s") from None
        raise JoinError("the hub closed the connection without an answer")


def env_values(ws_url: str, joined: Dict[str, Any]) -> Dict[str, str]:
    c = joined["credentials"]
    return {
        "BRIEFLING_WS_URL": ws_url,
        "BRIEFLING_TOKEN_URL": c["token_url"],
        "BRIEFLING_CLIENT_ID": c["client_id"],
        "BRIEFLING_CLIENT_SECRET": c["client_secret"],
        "BRIEFLING_SPACES": "*",
        "BRIEFLING_ALLOWED_USERS": ",".join(str(a) for a in joined["allowed"]),
        "BRIEFLING_HOME_CHANNEL": "off",
    }


def already_joined() -> bool:
    from hermes_cli.config import get_env_path, invalidate_env_cache, load_env
    invalidate_env_cache()
    return get_env_path().exists() and bool(load_env().get("BRIEFLING_CLIENT_ID", "").strip())


def write_env(values: Dict[str, str]) -> str:
    """Write the keys with Hermes' own .env writer (atomic, profile-scoped), then make sure the file is 0600
    (Hermes keeps an existing file's mode and skips chmod in containers)."""
    from hermes_cli import config as hc
    check = getattr(hc, "require_env_writable", None)
    if check:  # refuse before writing anything rather than leave half the keys
        for key in values:
            try:
                check(key, "set")
            except ValueError:
                raise JoinError(f"Hermes refuses to write {key} in this profile's .env") from None
    for key, value in values.items():
        hc.save_env_value(key, value)
    path = hc.get_env_path()
    if not (hasattr(hc, "is_managed") and hc.is_managed()):  # managed installs set their own group-readable mode
        os.chmod(path, 0o600)
    return str(path)


def _restart_hint() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = get_active_profile_name()
    except Exception:
        name = "default"
    if name == "default":
        return "hermes gateway restart"
    if name == "custom":
        from hermes_constants import get_hermes_home
        return f"HERMES_HOME={get_hermes_home()} hermes gateway restart"
    return f"hermes -p {name} gateway restart"


def cmd_join(args: argparse.Namespace) -> int:
    try:
        app, code = parse_target(args.target, args.app)
        if not args.force and already_joined():
            print("briefling join: this profile is already connected to briefling (BRIEFLING_CLIENT_ID is set in "
                  "its .env). Use another profile, or --force to replace it.", file=sys.stderr)
            return 1
        ws_url, joined = asyncio.run(redeem(app, code, TIMEOUT_S))
        path = write_env(env_values(ws_url, joined))
    except JoinError as e:
        print(f"briefling join: {e}", file=sys.stderr)
        return 1
    spaces = ", ".join(str(s) for s in joined["spaces"]) or "(none yet)"
    print(f"Joined briefling as agent {joined['agent']!r} (spaces: {spaces}).")
    print(f"Credentials written to {path}.")
    print(f"Next step: restart the gateway to connect: {_restart_hint()}")
    return 0


def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="briefling_command", metavar="COMMAND")
    j = sub.add_parser("join", help="Redeem an agent invitation and write the credentials into this profile's .env",
                       description="Redeem a briefling agent invitation (the link the admin sent, or its code) and "
                                   "write the hub settings and credentials into the active profile's .env. "
                                   "Restart the gateway afterwards.")
    j.add_argument("target", metavar="CODE_OR_LINK", help="invitation link <AppUrl>/agent.md?code=... or the bare code")
    j.add_argument("--app", help="briefling app URL (required with a bare code)")
    j.add_argument("--force", action="store_true", help="replace the briefling credentials already in this profile")
    parser.set_defaults(_briefling_parser=parser)


def cli(args: argparse.Namespace) -> Optional[int]:
    if getattr(args, "briefling_command", None) == "join":
        return cmd_join(args)
    args._briefling_parser.print_help()
    return None
