"""perms.py: what a refused permission names (never the command line) and the config line per agent."""
from briefling_acp import perms


def test_program_keeps_the_program_and_subcommand_only():
    assert perms.program_of("TOKEN=abc git push https://x:tok@github.com/a/b HEAD") == "git push"
    assert perms.program_of("cd repo && npm test && gh pr create --title x") == "gh pr"
    assert perms.program_of(["glab", "mr", "create"]) == "glab mr"
    assert perms.program_of("sudo rm -rf /") == "rm"
    assert perms.program_of("") == ""


def test_refused_kinds():
    ex = {"toolCall": {"kind": "execute", "title": "x", "rawInput": {"command": "git push origin main"}}}
    assert perms.refused_of(ex, False) == ("shell", "git push")
    assert perms.refused_of({"toolCall": {"kind": "edit", "title": "Write a.py"}}, False) == ("edit", "file edits")
    assert perms.refused_of({"toolCall": {"title": "@briefling/turn_outcome"}}, True)[0] == "briefling"
    assert perms.refused_of({"toolCall": {"kind": "fetch", "title": "Fetch https://x"}}, False)[0] == "tool"


def test_agent_of_the_bridge_command():
    assert perms.agent_of(["npx", "-y", "@agentclientprotocol/claude-agent-acp@0.107.3"]) == "claude"
    assert perms.agent_of(["/usr/bin/copilot", "--acp", "--stdio"]) == "copilot"
    assert perms.agent_of(["qwen", "--acp"]) == "qwen"
    assert perms.agent_of(["kiro-cli", "acp"]) == "kiro"
    assert perms.agent_of(["python", "fake_agent.py"]) == ""


def test_fix_lines_name_the_config():
    assert perms.fix_line("claude", "shell", "git push") == 'add "Bash(git push *)" to permissions.allow in ~/.claude/settings.json (or the repo\'s .claude/settings.json)'
    assert "shell(gh:*)" in perms.fix_line("copilot", "shell", "gh pr")
    assert '"Bash(glab mr *)"' in perms.fix_line("qwen", "shell", "glab mr")
    assert "mcp__briefling" in perms.fix_line("claude", "briefling", "briefling's tools")
    assert "allowedCommands" in perms.fix_line("kiro", "shell", "git push")
    assert perms.fix_line("", "shell", "a shell command").startswith("allow it")
