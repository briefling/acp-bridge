"""A refused permission during a board task (flow): what was refused, said without the command line, and the line to
add to the agent's own configuration so that it runs unattended next time.

The bridge refuses every ACP permission request (acp.py `reject_option`): what an agent may do alone is decided by its
own configuration, never by the bridge. An agent whose config does not allow git push / gh / glab would therefore stop
at the same wall on every task; the flow host marks the task blocked with this cause and pauses the agent instead.
Only the program and its subcommand are kept (`git push`, `gh pr`): the rest of a command line may hold a secret
(`TOKEN=… git push https://…`), and the cause is shown on the board and in briefling.
The config lines come from each vendor's documentation (not tried live for every agent): acp-bridge/README.md.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

WORD_RE = re.compile(r"^[a-z][a-z0-9._-]{0,30}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SUB = {"git", "gh", "glab", "npm", "pnpm", "yarn", "uv", "cargo", "go", "docker", "make"}  # programs whose subcommand matters
WRAP = {"sudo", "env", "command", "exec", "nohup", "time"}


def agent_of(command: List[str]) -> str:
    """The agent the bridge runs, from its command line (hub/src/localagents.ts CANDIDATES), '' when unknown."""
    words = " ".join(re.split(r"[\\/]", w)[-1].lower() for w in command or [])
    for key, pat in (("claude", r"claude-agent-acp|\bclaude\b"), ("codex", r"codex"), ("copilot", r"\bcopilot\b"),
                     ("cursor", r"cursor-agent|^agent\b"), ("antigravity", r"\bagy\b"), ("gemini", r"\bgemini\b"),
                     ("kiro", r"kiro"), ("qwen", r"\bqwen\b"), ("opencode", r"opencode"), ("goose", r"\bgoose\b"),
                     ("hermes", r"\bhermes\b"), ("vibe", r"vibe"), ("pi", r"pi-acp|^pi\b"), ("maki", r"\bmaki\b")):
        if re.search(pat, words):
            return key
    return ""


def _words(cmd: Any) -> List[str]:
    if isinstance(cmd, list):
        return [str(x) for x in cmd]
    try:
        return shlex.split(str(cmd or ""))
    except ValueError:
        return str(cmd or "").split()


def program_of(cmd: Any) -> str:
    """`git push`, `gh pr`, `glab mr`, `npm`... : the program (and subcommand) of a command, never its arguments. The
    first segment of a pipeline / `&&` chain that is a git, gh or glab call wins (that is what the flow needs)."""
    text = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
    found = ""
    for seg in re.split(r"&&|\|\||[;|\n]", text):
        ws = [w for w in _words(seg)]
        while ws and (ENV_RE.match(ws[0]) or ws[0] in WRAP or ws[0].startswith("-")):
            ws.pop(0)
        if not ws:
            continue
        prog = re.split(r"[\\/]", ws[0])[-1].lower()
        if not WORD_RE.match(prog):
            continue
        sub = next((w.lower() for w in ws[1:] if not w.startswith("-")), "") if prog in SUB else ""
        label = prog + (" " + sub if sub and WORD_RE.match(sub) else "")
        if prog in ("git", "gh", "glab"):
            return label
        found = found or label
    return found


def refused_of(p: Dict[str, Any], briefling_tool: bool) -> Tuple[str, str]:
    """(kind, what) of a session/request_permission: kind = shell | briefling | edit | tool; what = `git push`, the
    tool's name... (never a command line)."""
    tc = p.get("toolCall") if isinstance(p.get("toolCall"), dict) else {}
    raw = tc.get("rawInput") if isinstance(tc.get("rawInput"), dict) else {}
    kind = str(tc.get("kind") or "")
    title = str(tc.get("title") or "")
    if briefling_tool:
        return "briefling", "briefling's tools"
    cmd = raw.get("command") or raw.get("cmd") or raw.get("commandLine")
    if kind == "execute" or cmd:
        prog = program_of(cmd if cmd else title)
        return "shell", prog or "a shell command"
    if kind in ("edit", "delete", "move"):
        return "edit", "file edits"
    name = re.sub(r"[^A-Za-z0-9_./@:-]+", " ", title).strip().split(" ")[0][:40] if title else ""
    return "tool", name or kind or "a tool"


def _pattern(what: str) -> str:
    """`git push` -> `git push`; `a shell command` -> '' (no pattern to give)."""
    return what if what != "a shell command" and all(WORD_RE.match(w) for w in what.split(" ")) else ""


def fix_line(agent: str, kind: str, what: str) -> str:
    """The line to add to the agent's own config so this runs unattended (one line, the config file named)."""
    pat = _pattern(what) if kind == "shell" else ""
    prog = pat.split(" ")[0] if pat else ""
    if kind == "briefling":
        return {
            "claude": 'add "mcp__briefling" to permissions.allow in ~/.claude/settings.json',
            "qwen": 'add "mcp__briefling" to permissions.allow in ~/.qwen/settings.json',
            "kiro": 'add "@briefling" to allowedTools in ~/.kiro/agents/<agent>.json',
            "antigravity": 'add "mcp(briefling/*)" to allow in ~/.gemini/antigravity-cli/settings.json',
            "copilot": "start copilot with --allow-tool='briefling' (bridge command: -- copilot --acp --stdio --allow-tool='briefling')",
        }.get(agent, "allow the briefling MCP server's tools in the agent's own configuration (acp-bridge/README.md)")
    if kind == "shell" and pat:
        return {
            "claude": 'add "Bash(%s *)" to permissions.allow in ~/.claude/settings.json (or the repo\'s .claude/settings.json)' % pat,
            "qwen": 'add "Bash(%s *)" to permissions.allow in ~/.qwen/settings.json' % pat,
            "copilot": "start copilot with --allow-tool='shell(%s:*)' (bridge command: -- copilot --acp --stdio --allow-tool='shell(%s:*)')" % (prog, prog),
            "cursor": 'add "Shell(%s)" to permissions.allow in ~/.cursor/cli-config.json' % prog,
            "gemini": 'add "run_shell_command(%s)" to tools.allowed in ~/.gemini/settings.json' % pat,
            "opencode": 'add {"action": "shell", "resource": "%s *", "effect": "allow"} to permissions in opencode.jsonc' % pat,
            "codex": 'set approval_policy = "never", sandbox_mode = "workspace-write" and [sandbox_workspace_write] network_access = true in ~/.codex/config.toml',
            "kiro": 'add "%s .*" to toolsSettings.execute_bash.allowedCommands in ~/.kiro/agents/<agent>.json' % pat,
        }.get(agent, "allow `%s` in the agent's own configuration (acp-bridge/README.md)" % pat)
    if kind == "edit":
        return {
            "claude": 'add "Edit" to permissions.allow in ~/.claude/settings.json (or run it with defaultMode acceptEdits)',
            "qwen": 'add "Edit" to permissions.allow in ~/.qwen/settings.json',
            "copilot": "start copilot with --allow-tool='write'",
            "codex": 'set sandbox_mode = "workspace-write" and approval_policy = "never" in ~/.codex/config.toml',
        }.get(agent, "allow file edits in the agent's own configuration (acp-bridge/README.md)")
    return "allow it in the agent's own configuration (acp-bridge/README.md)"


def cause(command: List[str], p: Dict[str, Any], briefling_tool: bool) -> Tuple[str, str]:
    """(what, fix) for a refused permission of the agent run by `command`."""
    kind, what = refused_of(p, briefling_tool)
    return what, fix_line(agent_of(command), kind, what)


def note(agent_label: Optional[str], what: str, fix: str) -> str:
    """The blocked status note (board) and the pause reason (briefling)."""
    who = agent_label or "the agent"
    return ("The bridge refused `%s`: %s's own configuration does not allow it, and the bridge never grants a "
            "permission. To fix: %s. Then Resume the agent in briefling (Board)." % (what, who, fix))
