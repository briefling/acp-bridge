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
pipx install "git+https://github.com/briefling/acp-bridge@v0.3.42"
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
pipx: `python3 -m venv ~/.venvs/briefling-acp && ~/.venvs/briefling-acp/bin/pip install "git+https://github.com/briefling/acp-bridge@v0.3.42"`.

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
- **Tool permissions**: every permission request of the agent is refused with the agent's own « reject » option, and
  the reply says which one (« Refused (not pre-authorized...): ... »). What the agent may do unattended is decided by
  its own configuration (Kiro: the allowed tools of its agent config), never by the bridge. `--trust-all-tools` is
  refused. The bridge advertises no file system or terminal capability to the agent.
- **Stop button**: cancels the running turn (`session/cancel`).
- **One turn at a time per conversation**: a message sent during a turn is handled after it.
- **Sessions**: one per conversation, kept across restarts in `<env file>.sessions.json` (resumed with `session/load`
  when the agent supports it, else a new session starts).
- **Not relayed**: attachments (the agent is told how many it missed), questions with buttons, cost per thread, the
  board/flow tools and the memory review turns (those need the Hermes plugin).
- **Kiro specifics**: `kiro-cli acp` refuses to start when not logged in; in a terminal the bridge then runs
  `kiro-cli login` once and retries, otherwise (service manager) it exits with Kiro's message.
  The bridge only uses standard ACP methods (`initialize`, `session/new|load|prompt|cancel`, `session/update`,
  `session/request_permission`), which Kiro documents for its default engine and for `--agent-engine=v3`; none of the
  Kiro-specific extensions.

## briefling's tools

The agent gets the same tools as a Hermes agent, with the same descriptions: `turn_outcome`, `propose_thread`, `propose_plan`,
`propose_issue`, `propose_space_about`, `acquire_resource` / `release_resource`, `pushed_commits`, `memory_search`,
`memory_read`, `memory_propose`, `radar_signal` (Radar passes), `radar_profile` (Radar profile pass). It also gets the same context with each message (space description, shared
resources, attached repos, Lounge rules).

How: in `session/new` the bridge declares one MCP server named `briefling` (`briefling-acp mcp`, stdio). Each call goes
from that server to the bridge over a Unix socket in `~/.config/briefling-acp/run/` (directory mode 700, socket 600;
Windows: 127.0.0.1 on a free port, every call carrying a random key that the bridge writes at the socket's place, in
that directory of the user's profile),
with a random token per conversation: a call only counts in its own conversation, while a turn of a trusted human
runs there. The hub checks everything again (turn, membership, space settings). Nothing listens on the network.

**One-time setup in Kiro**: like any tool, these calls ask for a permission, and the bridge refuses every permission
request. Allow them once in Kiro's config (the reply says so when they are refused):

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

## Tests

```sh
python -m pytest -q          # from the repo root (`acp-bridge/` in the briefling repo); needs aiohttp, pytest, pytest-asyncio
```

`tests/fake_agent.py` is a scripted ACP agent; the tests run the bridge against a fake hub.

The source lives in the briefling repository (`acp-bridge/`, live check `scripts/e2e-acp.sh`), published here by
`scripts/publish-acp-bridge.ts`; `briefling_acp/join.py` and `briefling_acp/tooldefs.py` are the Hermes plugin's own files.
