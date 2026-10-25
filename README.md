# briefling ACP bridge

Connects an agent that speaks the [Agent Client Protocol](https://agentclientprotocol.com) (ACP) to a briefling hub,
without Hermes: [Kiro CLI](https://kiro.dev/docs/cli/acp/) (`kiro-cli acp`), or any other ACP agent (only the command
changes). The bridge is a small Python daemon: it joins the hub with an invitation link, starts the agent command, opens
one ACP session per briefling conversation, relays each message of a trusted human as a prompt and streams the agent's
reply back as one message per turn.

## Install and start

On the agent's machine, with Python 3.11+ and [pipx](https://pipx.pypa.io) (`brew install pipx` on macOS):

```sh
pipx install "git+https://github.com/briefling/acp-bridge@v0.2.0"
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
pipx: `python3 -m venv ~/.venvs/briefling-acp && ~/.venvs/briefling-acp/bin/pip install "git+https://github.com/briefling/acp-bridge@v0.2.0"`.

## What the bridge does, and does not

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
- **Not relayed (v1)**: attachments (the agent is told how many it missed), questions with buttons, briefling's agent
  tools (`propose_thread`, `turn_outcome`, resources, project memory), cost per thread.
- **Kiro specifics**: `kiro-cli acp` refuses to start when not logged in; in a terminal the bridge then runs
  `kiro-cli login` once and retries, otherwise (service manager) it exits with Kiro's message.
  The bridge only uses standard ACP methods (`initialize`, `session/new|load|prompt|cancel`, `session/update`,
  `session/request_permission`), which Kiro documents for its default engine and for `--agent-engine=v3`; none of the
  Kiro-specific extensions.

## Tests

```sh
cd acp-bridge && python -m pytest -q          # needs aiohttp, pytest, pytest-asyncio
```

`tests/fake_agent.py` is a scripted ACP agent; the tests run the bridge against a fake hub.

The source lives in the briefling repository (`acp-bridge/`, live check `scripts/e2e-acp.sh`), published here by
`scripts/publish-acp-bridge.ts`; `briefling_acp/join.py` is the Hermes plugin's `join.py`.
