# briefling ACP bridge

Connects an agent that speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP) to a briefling hub,
without Hermes: [Kiro CLI](https://kiro.dev/docs/cli/acp/) (`kiro-cli acp`), or any other ACP agent (only the command
changes). The bridge is a small Python daemon: it joins the hub with an invitation link, starts the agent command, opens
one ACP session per briefling conversation, relays each message of a trusted human as a prompt and streams the agent's
reply back as one message per turn. The agent also gets briefling's tools (cards, turn outcome, shared resources,
project memory) through a local MCP server the bridge declares in each session.

## Install and start

On the agent's machine, with Python 3.11+ and [pipx](https://pipx.pypa.io) (`brew install pipx` on macOS):

```sh
pipx install "git+https://github.com/briefling/acp-bridge@v0.3.67"
cd <the folder Kiro will work in>
briefling-acp "<invitation link>"
```

That is all. The first run joins the hub with the link (credentials saved in `~/.config/briefling-acp/bridge.env`,
mode 600), opens `kiro-cli login` if Kiro is not logged in yet, then starts `kiro-cli acp`. Next times, from the same
folder: `briefling-acp`. Stop it with Ctrl-C.

The link comes from an admin of the space: space settings, **Invite an agent**, the agent's name in lower case (e.g.
`kiro`), **Copy** (one use, 24 hours). Every person who should drive the agent must be **Trusted** in the space before
the join: the bridge only answers them. In the space settings the invitation then shows **Connected**; in the Lounge,
`@kiro hello` gets an answer.

Options: `--cwd DIR` (the agent's folder, default the current one), `--env FILE` (another credentials file, one per
agent), `--force` (join again), `-- <command>` for another agent or Kiro options (default `kiro-cli acp`, e.g.
`-- kiro-cli acp --agent my-agent`). The `BRIEFLING_*` variables of the process environment override the file. Without
pipx: `python3 -m venv ~/.venvs/briefling-acp && ~/.venvs/briefling-acp/bin/pip install "git+https://github.com/briefling/acp-bridge@v0.3.67"`.

**Behind a proxy**: the bridge's connections to the hub (invitation, token, WebSocket) use the machine's proxy:
`HTTPS_PROXY` (also for `wss://`), `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY`; without them, the system settings
(Windows Internet Options, macOS System Settings). 127.0.0.1 and localhost never go through the proxy.

**Updates**: the bridge updates itself. At each connection (and right after the hub is deployed) the hub announces
the bridge release it ships; when it is newer, the bridge checks that the public tag `v<version>` is that very commit,
installs it with the installer that installed it (pipx, a venv's pip, `uv tool install` or a uv venv), checks the new
code imports and reports that version, then restarts itself with the same arguments once no turn runs (conversations
resume). A failed install puts the previous version back and is never retried; never a downgrade. The space's General
feed shows one line after each update. Not automatic, and the space's leads see why when the bridge is behind:
`BRIEFLING_ACP_AUTO_UPDATE=0` (environment or credentials file), a bridge run by `uvx --from …@<tag>` (the launcher
picks the version: change the tag there), a checkout or editable install. The update record lives next to the
credentials file (`bridge.env.update.json`). Under a service manager (launchd, systemd), the restart keeps the same
process: nothing to configure.

## What the bridge does, and does not

How a turn and a tool call flow through the bridge, and what to check when Kiro answers « Refused »:
[docs/kiro-integration.md](docs/kiro-integration.md).


- **Who can drive the agent**: only the humans listed in `BRIEFLING_ALLOWED_USERS` (the space's trusted humans at join
  time, Cognito ids). A message or a Stop from anyone else is dropped. Someone made trusted later must be added there.
- **Tool permissions**: by default every permission request of the agent is refused with the agent's own « reject »
  option, and the reply says which one (« Refused (not pre-authorized...): ... »). What the agent may do unattended is decided by
  its own configuration (Kiro: the allowed tools of its agent config). For human-driven turns, an operator can enable
  one-shot approval cards (below). `--trust-all-tools` is refused. The bridge advertises no file system or terminal
  capability to the agent.
- **Stop button**: cancels the running turn (`session/cancel`).
- **One turn at a time per conversation**: a message sent during a turn is handled after it.
- **Sessions**: one per conversation, kept across restarts in `<env file>.sessions.json` (resumed with `session/load`
  when the agent supports it, else a new session starts).
- **Attachments**: each one is listed at the top of the message (id, name, type, size, who posted it). When the
  agent's `initialize` advertises it (`promptCapabilities`), images (png, jpeg, gif, webp, 5 MB each, 20 MB per
  message) come in the prompt as image blocks (`image`) and `.txt` files (a long paste) as embedded text, 100 KB per
  message (`embeddedContext`). The others, or the same ones later, through the `attachment_get(id)` tool: the bridge
  asks the hub for a fresh read URL (only files of the spaces the agent serves and the turn's human sees), downloads
  it and answers with the text (first 100 KB) or the image. PDFs and archives are not readable this way. No local path
  is handed out: the agent may run where the bridge's disk is not visible.
- **Cards and mails**: the agent can propose an applet (`applet_propose`, after `applet_list` to update one) and a
  reminder (`reminder_propose`, a one-shot date/time card a member adds), as a Hermes agent does. The mail tools
  (`mail_import` in a lead's turn, `exchange_read` / `exchange_summary` in the hidden summary run) are listed only in
  a space whose Exchanges module is on (the hub's deliver says `exchanges: false` otherwise), so they cost nothing
  elsewhere.
- **Capabilities**: at every connection the bridge announces the names of the tools above (`subscribe.bridge.tools`); Settings › Agents shows them as features.
- **Not relayed**: general agent questions with buttons (apart from permission cards), cost per thread, the memory
  review turns, preview sharing, the voice channel, `secret_totp` and `request_elevation` (those need the Hermes
  plugin).
- **Board flow**: the bridge runs the same flow host as the Hermes plugin (`flow/`, `flowhost.py`, `flowrunner.py`,
  the plugin's files). In a board-driven space where briefling pilots the agent (by default when this machine has no
  unstrikeable state for it), a board task (`dev:<agent>` label + Ready) becomes a turn in the issue's thread, prompted
  by the flow; the turn's end (`stopReason`) ticks the board at once. The agent gets `board_status`, `board_move`,
  `board_comment`, `board_label`, `board_set` and `flow_switch`. ACP reports no cost: set the caps in tasks per day
  (`max_events_per_day`) and runs per task in Settings › Work, the cost caps never trigger. The flow state lives next
  to the credentials (`bridge.env.flow/`); `BRIEFLING_FLOW` (settings file) and `BRIEFLING_FLOW_AUTO=0` work as for
  the plugin. The agent must be allowed, in its own configuration, to run git push / gh / glab: automatic tasks refuse
  every permission request (see **Minimal config for the board flow** below). At the first refused command, edit or
  briefling tool of a task, the bridge sets the task's status to blocked on the board (what was refused and the line to
  add to the agent's config), pauses the agent (every next task would hit the same wall) and reports the cause to the
  hub: Settings › Work › Workflow shows it in red. Only the program and subcommand are named (`git push`), never the
  command line. Fix the config, then **Resume** the agent on the Board.
- **Kiro specifics**: `kiro-cli acp` refuses to start when not logged in; in a terminal the bridge then runs
  `kiro-cli login` once and retries, otherwise (service manager) it exits with Kiro's message.
  The bridge only uses standard ACP methods (`initialize`, `session/new|load|prompt|cancel`, `session/update`,
  `session/request_permission`), which Kiro documents for its default engine and for `--agent-engine=v3`; none of the
  Kiro-specific extensions.

## One-shot permission cards

On by default since 0.3.63; turn it off with `BRIEFLING_ACP_APPROVALS=0` in the bridge's environment or credentials
file, then restart it (every permission request is then refused, as before). No agent configuration or permanent
permission is changed.

During a trusted human's turn, an ACP permission request that offers `allow_once` becomes a briefling approval card
with **Approve** (this call only) and **Deny**. The bridge creates the card directly: it still works when the agent
has not pre-authorized briefling's MCP tools. A trusted human's answer to that exact card resumes the suspended ACP
request in its original session; it does not start a new prompt. Other conversations continue while it waits.

Only the tool or program is shown, never raw arguments, command chains, output or file contents: those can contain
credentials. This summary is not the full action. Review the action on the agent host before approving if unsure;
approval covers the entire pending call, not just its displayed program.

Requests expire after five minutes. **Deny**, any other answer, a new chat message in the conversation, Stop, hub
disconnection, session replacement or bridge/agent restart cannot grant permission. Approvals are held in memory
only; expired or replayed card answers never start another turn. `allow_always` is never selected. Automatic board
tasks retain the existing refusal/block/pause behavior; pre-authorize their tools as below. The space's command
approval mode (Settings › Agents: agent, always, contributor) does not apply to these cards: only a trusted human
answers them.

## briefling's tools

The agent gets the same tools as a Hermes agent, with the same descriptions: `turn_outcome`, `propose_thread`, `propose_plan`,
`propose_issue`, `propose_space_about`, `acquire_resource` / `release_resource`, `pushed_commits`, `memory_search`,
`memory_read`, `memory_propose`, `radar_signal` (Radar passes), `radar_clear` (managed Radar pass), `radar_profile` (Radar profile pass), plus its own
`attachment_get` (see Attachments above). It also gets the same context with each message (space description, shared
resources, attached repos, Lounge rules).

How: in `session/new` the bridge declares one MCP server named `briefling` (`briefling-acp mcp`, stdio). Each call goes
from that server to the bridge over a Unix socket in `~/.config/briefling-acp/run/` (directory mode 700, socket 600;
Windows: 127.0.0.1 on a free port, every call carrying a random key that the bridge writes at the socket's place, in
that directory of the user's profile),
with a random token per conversation: a call only counts in its own conversation, while a turn of a trusted human
runs there. The hub checks everything again (turn, membership, space settings). Nothing listens on the network.

**One-time setup in Kiro**: like any tool, these calls ask for a permission, refused by default. To run unattended
without an approval card on each call, allow them in Kiro's config (the reply says so when they are refused):

- Kiro CLI 2.x: add `"@briefling"` to `allowedTools` of the agent you run (`kiro-cli acp --agent <name>`, file
  `~/.kiro/agents/<name>.json`).
- Kiro CLI V3: in `~/.kiro/settings/permissions.yaml`:
  ```yaml
  rules:
    - capability: mcp
      match: ["briefling/*"]
      effect: allow
  ```
  and trust the agent's folder once (an untrusted workspace asks before every MCP call).

**If the agent ignores the servers of `session/new`** (the tools never show up; with Kiro the bridge log shows
`MCP server ready: briefling` when they do): declare the server in the agent's own config instead, e.g.
`"mcpServers": {"briefling": {"command": "briefling-acp", "args": ["mcp"]}}` (add `"--env", "<file>"` if you use
`--env`). Without a token, a call is tied to the only turn running: tools then work for one conversation at a time.

**Agent in a microVM** (Docker Sandboxes `sbx`, any agent that cannot start a command on the host nor open its Unix
socket): run the bridge with `--mcp-port <port> --mcp-host host.docker.internal`. The bridge then also serves the
tools as an MCP Streamable HTTP server on `127.0.0.1:<port>/mcp` and, to an agent that advertises
`mcpCapabilities.http` (Claude Code through `claude-agent-acp` does), declares it in `session/new` and `session/load`
as an `http` server with the conversation's token in an `Authorization: Bearer` header: same per-conversation binding
as the stdio server, no extra config inside the VM. Allow that one port in the sandbox network policy, scoped to the
sandbox: `sbx policy allow network --sandbox <name> localhost:<port>` (the sandbox's host proxy turns
`host.docker.internal` into `localhost`). Gates: loopback only, a request without a known token gets 401 (no
« only turn running » fallback over a port), a request with an `Origin` header (a browser page) gets 403, POST only.
An agent that does not take HTTP servers keeps getting the stdio one.

## Minimal config for the board flow

A board task needs, without asking: file edits in the repo, `git` (commit, push), the forge's CLI (`gh` for GitHub,
`glab` for GitLab) and briefling's tools. Each agent decides this in its own config, never the bridge.

**Launched by `npx briefling` (standalone server)**: it is done for you, the agent may then edit files, run any command
and call briefling's tools without asking (`hub/src/agentallow.ts`; `BRIEFLING_AGENT_ALLOW=0` on the server turns it
off). Per agent:

| Agent | How |
|---|---|
| GitHub Copilot CLI | launch options `--allow-tool=shell --allow-tool=write --allow-tool=briefling` |
| Qwen Code | launch option `--allowed-tools=Edit,Bash,mcp__briefling` |
| Codex (`codex-acp`) | env `INITIAL_AGENT_MODE=agent-full-access` (kept when you set it yourself) |
| Claude Code | merged once into `~/.claude/settings.json` `permissions.allow`: `Edit`, `Write`, `Bash`, `mcp__briefling` |
| Cursor | merged once into `~/.cursor/cli-config.json` `permissions.allow`: `Write(**)`, `Shell(*)`, `Mcp(briefling:*)` |
| Antigravity CLI | merged once into `~/.gemini/antigravity-cli/settings.json` `permissions.allow`: `write_file(*)`, `command(*)`, `mcp(briefling/*)` |
| Gemini CLI | its own policy file `~/.gemini/policies/briefling.toml` (write_file, replace, run_shell_command, MCP server `briefling`) |
| Kiro CLI V3 | `~/.kiro/settings/permissions.yaml` when absent (fs_write, shell, mcp `briefling/*`); an existing file is left as it is |
| others (Hermes, opencode, goose, maki, pi, vibe) | nothing: their own settings (table below) |

« Merged once »: only the missing entries are added, every other key is kept, the previous file is copied to
`<file>.briefling-bak`, and it happens at the first launch of that agent only (marker `<agent>.allowed` in the server's
`agents/` folder): a rule you remove afterwards is not put back. A file that is not plain JSON (comments) is left
untouched; the server's log names what to add. Kiro CLI 2.x reads `~/.kiro/agents/<agent>.json`, not touched: see below.

By hand (an agent you run yourself with `briefling-acp`): The lines below
come from each vendor's documentation (Nov 2026) and were not all tried live; the blocked status names the line for
the agent that hit the wall.

| Agent | File | Minimal lines |
|---|---|---|
| Claude Code (`claude-agent-acp`) | `~/.claude/settings.json` (or the repo's `.claude/settings.json`) | `"permissions": {"allow": ["Edit", "Bash(git *)", "Bash(gh *)", "Bash(glab *)", "mcp__briefling"]}` |
| GitHub Copilot CLI | bridge command (the CLI options; approvals saved in `~/.copilot/permissions-config.json` are per folder) | `-- copilot --acp --stdio --allow-tool='write' --allow-tool='shell(git:*)' --allow-tool='shell(gh:*)' --allow-tool='shell(glab:*)' --allow-tool='briefling'` (or `--allow-tool=shell` for any command) |
| Qwen Code | `~/.qwen/settings.json` | `"permissions": {"allow": ["Edit", "Bash(git *)", "Bash(gh *)", "Bash(glab *)", "mcp__briefling"]}` |
| Codex (`codex-acp`) | `~/.codex/config.toml` | `approval_policy = "never"`, `sandbox_mode = "workspace-write"`, `[sandbox_workspace_write]` `network_access = true` (push needs the network) |
| Cursor | `~/.cursor/cli-config.json` | `"permissions": {"allow": ["Write(**)", "Shell(git)", "Shell(gh)", "Shell(glab)"]}` |
| Gemini CLI | `~/.gemini/settings.json` | `"tools": {"allowed": ["run_shell_command(git)", "run_shell_command(gh)", "run_shell_command(glab)"]}`, plus `"mcpServers": {"briefling": {"trust": true}}` when declared there |
| Antigravity CLI | `~/.gemini/antigravity-cli/settings.json` | `"allow": ["mcp(briefling/*)"]`, plus its shell rules for git / gh / glab |
| opencode | `opencode.jsonc` | `"permissions": [{"action": "shell", "resource": "git *", "effect": "allow"}, {"action": "shell", "resource": "gh *", "effect": "allow"}, {"action": "shell", "resource": "glab *", "effect": "allow"}, {"action": "edit", "resource": "*", "effect": "allow"}]` |
| Kiro CLI 2.x | `~/.kiro/agents/<agent>.json` | `"allowedTools": ["fs_write", "execute_bash", "@briefling"]`, or narrower: `"toolsSettings": {"execute_bash": {"allowedCommands": ["git .*", "gh .*", "glab .*"]}}` |
| Kiro CLI V3 | `~/.kiro/settings/permissions.yaml` | an `mcp` rule for `briefling/*` (above) and a shell rule for git / gh / glab |

The forge's own login must work too, in the agent's environment: `gh auth status` / `glab auth status`, and a git
credential helper for the push. A blocked task names the refusal only; a failed login shows in the agent's reply.

## Tests

```sh
python -m pytest -q          # from the repo root (`acp-bridge/` in the briefling repo); needs aiohttp, pytest, pytest-asyncio
```

`tests/fake_agent.py` is a scripted ACP agent; the tests run the bridge against a fake hub.

The source lives in the briefling repository (`acp-bridge/`, live check `scripts/e2e-acp.sh`), published here by
`scripts/publish-acp-bridge.ts`; `briefling_acp/join.py` and `briefling_acp/tooldefs.py` are the Hermes plugin's own files.
