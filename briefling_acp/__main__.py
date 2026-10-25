"""CLI: `briefling-acp join <link> [--env FILE]`, then `briefling-acp run [--env FILE] [--cwd DIR] -- <agent command>`
(or `python -m briefling_acp ...`).

The env file holds the hub URL and this agent's own credentials (Cognito client, written by `join`, mode 0600) and
the trusted humans' ids. The client secret is never printed or logged.
"""

import argparse
import asyncio
import logging
import os
import shlex
import sys
from typing import Dict

from . import join

DEFAULT_ENV = os.path.join(os.path.expanduser("~"), ".config", "briefling-acp", "bridge.env")
KEYS = ("BRIEFLING_WS_URL", "BRIEFLING_TOKEN_URL", "BRIEFLING_CLIENT_ID", "BRIEFLING_CLIENT_SECRET", "BRIEFLING_ALLOWED_USERS")


def read_env(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_env(path: str, values: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# briefling ACP bridge: hub URL and this agent's credentials. Keep it private.\n")
        for k in KEYS:
            f.write(f"{k}={values.get(k, '')}\n")
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)


def cmd_join(args: argparse.Namespace) -> int:
    if os.path.exists(args.env) and not args.force:
        print(f"join: {args.env} already exists (an agent already joined). Use --force to replace it.", file=sys.stderr)
        return 1
    try:
        app, code = join.parse_target(args.target, args.app)
        ws_url, joined = asyncio.run(join.redeem(app, code, join.TIMEOUT_S))
    except join.JoinError as e:
        print(f"join: {e}", file=sys.stderr)
        return 1
    write_env(args.env, join.env_values(ws_url, joined))
    spaces = ", ".join(str(s) for s in joined["spaces"]) or "(none yet)"
    trusted = len(joined["allowed"])
    print(f"Joined briefling as agent {joined['agent']!r} (spaces: {spaces}).")
    print(f"Credentials written to {args.env} (mode 600). {trusted} trusted human(s) may drive the agent.")
    print(f"Next: briefling-acp run --env {shlex.quote(args.env)} -- kiro-cli acp")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .bridge import Bridge
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("run: give the agent command after --, e.g. `-- kiro-cli acp`", file=sys.stderr)
        return 2
    if any(a in ("--trust-all-tools", "-a") for a in command):
        print("run: refusing --trust-all-tools: pre-authorize tools in the agent's own config instead", file=sys.stderr)
        return 2
    try:
        # The process environment overrides the file (containers, service managers, the live test).
        conf = {**(read_env(args.env) if os.path.exists(args.env) else {}), **{k: os.environ[k] for k in KEYS if k in os.environ}}
        bridge = Bridge(conf, command, os.path.abspath(args.cwd), state_path=args.env + ".sessions.json")
    except (OSError, ValueError) as e:
        print(f"run: {e}", file=sys.stderr)
        return 1
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="briefling-acp", description="Bridge an ACP agent (Kiro, ...) to briefling.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("join", help="redeem an agent invitation link and write the env file")
    j.add_argument("target", metavar="CODE_OR_LINK")
    j.add_argument("--app", help="briefling app URL (required with a bare code)")
    j.add_argument("--env", default=DEFAULT_ENV, help=f"env file to write (default {DEFAULT_ENV})")
    j.add_argument("--force", action="store_true", help="replace an existing env file")
    r = sub.add_parser("run", help="connect to the hub and relay messages to the ACP agent")
    r.add_argument("--env", default=DEFAULT_ENV)
    r.add_argument("--cwd", default=".", help="working directory of the agent's sessions (default: current)")
    r.add_argument("command", nargs=argparse.REMAINDER, help="-- <agent command>, e.g. -- kiro-cli acp")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return cmd_join(args) if args.cmd == "join" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
