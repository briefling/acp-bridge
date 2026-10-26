"""CLI, one command:

    briefling-acp "<invitation link>"     first time: joins the hub, then runs
    briefling-acp                         next times: runs with the saved credentials
    briefling-acp mcp                     the MCP server of briefling's tools (started by the agent, not by hand)

Options: `--cwd DIR` (working directory of the agent's sessions, default the current one), `--env FILE` (credentials
file, one per agent), `--force` (join again over an existing file), `-- <agent command>` (default `kiro-cli acp`).

The env file holds the hub URL and this agent's own credentials (Cognito client, written at join, mode 0600) and the
trusted humans' ids. The client secret is never printed or logged. When Kiro is not logged in and the bridge runs in a
terminal, it starts `kiro-cli login` once, then retries.
"""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from typing import Dict, List, Optional

from . import join

DEFAULT_ENV = os.path.join(os.path.expanduser("~"), ".config", "briefling-acp", "bridge.env")
DEFAULT_COMMAND = ["kiro-cli", "acp"]
KEYS = ("BRIEFLING_WS_URL", "BRIEFLING_TOKEN_URL", "BRIEFLING_CLIENT_ID", "BRIEFLING_CLIENT_SECRET", "BRIEFLING_ALLOWED_USERS")
KIRO_LOGIN_HINT = "kiro-cli login"  # Kiro's own message when not logged in: « please log in with kiro-cli login »


def socket_path(env: str) -> str:
    """The bridge's Unix socket, next to the credentials file in a private `run/` directory (one per env file)."""
    env = os.path.abspath(env)
    return os.path.join(os.path.dirname(env), "run", os.path.basename(env) + ".sock")


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


def do_join(target: str, app: Optional[str], env: str, force: bool) -> int:
    if os.path.exists(env) and not force:
        print(f"briefling-acp: this agent already joined ({env} exists). Run `briefling-acp` without the link, "
              "or add --force to join again.", file=sys.stderr)
        return 1
    try:
        app_url, code = join.parse_target(target, app)
        ws_url, joined = asyncio.run(join.redeem(app_url, code, join.TIMEOUT_S))
    except join.JoinError as e:
        print(f"briefling-acp: {e}", file=sys.stderr)
        return 1
    write_env(env, join.env_values(ws_url, joined))
    spaces = ", ".join(str(s) for s in joined["spaces"]) or "(none yet)"
    print(f"Joined briefling as agent {joined['agent']!r} (spaces: {spaces}); {len(joined['allowed'])} trusted "
          f"human(s) may drive it. Credentials saved in {env}. Next time, just run `briefling-acp`.")
    return 0


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def do_run(env: str, command: List[str], cwd: str) -> int:
    from .bridge import Bridge
    if any(a in ("--trust-all-tools", "-a") for a in command):
        print("briefling-acp: refusing --trust-all-tools: pre-authorize tools in the agent's own config instead", file=sys.stderr)
        return 2
    # The process environment overrides the file (containers, service managers, the live test).
    conf = {**(read_env(env) if os.path.exists(env) else {}), **{k: os.environ[k] for k in KEYS if k in os.environ}}
    for attempt in (1, 2):
        try:
            bridge = Bridge(conf, command, os.path.abspath(cwd), state_path=env + ".sessions.json",
                            socket_path=socket_path(env))
        except ValueError:
            print('briefling-acp: not joined yet: run `briefling-acp "<invitation link>"` (the link the space admin '
                  "sent you).", file=sys.stderr)
            return 1
        try:
            asyncio.run(bridge.run())
            return 0
        except KeyboardInterrupt:
            return 0
        except SystemExit:
            if (attempt == 1 and KIRO_LOGIN_HINT in bridge.agent.stderr_tail
                    and os.path.basename(command[0]).startswith("kiro-cli") and interactive()):
                print("Kiro is not logged in: starting `kiro-cli login`...", file=sys.stderr)
                if subprocess.run([command[0], "login"]).returncode == 0:
                    continue
            return 1
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["mcp"]:  # stdout is the MCP channel: nothing else may print there
        from . import mcp
        p = argparse.ArgumentParser(prog="briefling-acp mcp", description="MCP server of briefling's tools (stdio).")
        p.add_argument("--env", default=DEFAULT_ENV, help=f"credentials file of the bridge (default {DEFAULT_ENV})")
        return mcp.main(socket_path(p.parse_args(argv[1:]).env))
    command = DEFAULT_COMMAND
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:] or DEFAULT_COMMAND
    p = argparse.ArgumentParser(prog="briefling-acp", description="Bridge an ACP agent (Kiro CLI, ...) to briefling. "
                                "First time: briefling-acp \"<invitation link>\". Then: briefling-acp.",
                                epilog="Agent command after --, default: kiro-cli acp (e.g. -- kiro-cli acp --agent my-agent).")
    p.add_argument("link", nargs="?", metavar="INVITATION_LINK", help="the link the space admin sent (first time only)")
    p.add_argument("--cwd", default=".", help="working directory of the agent (default: the current directory)")
    p.add_argument("--env", default=DEFAULT_ENV, help=f"credentials file (default {DEFAULT_ENV})")
    p.add_argument("--app", help="briefling app URL (only with a bare code instead of the link)")
    p.add_argument("--force", action="store_true", help="join again over an existing credentials file")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    if args.link:
        rc = do_join(args.link, args.app, args.env, args.force)
        if rc:
            return rc
    return do_run(args.env, command, args.cwd)


if __name__ == "__main__":
    sys.exit(main())
