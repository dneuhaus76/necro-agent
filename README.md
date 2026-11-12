# NECROMANCER (NECRO)

*A minimal terminal agent harness — Python 3 stdlib only.* 

**NECRO** is a small, self-contained agent harness for the terminal. It is inspired by
[pi-agent](https://github.com/earendil-works/pi) — an amazing product.

## Features

- **Tiny harness** — one main script plus a few modules, Python 3 standard library only, no dependencies to install
- **Automatic context size** — NECRO queries the inference server for the model's current context window (`--ctx-size`) at startup; no manual `ctx_window` needed
- **Telegram bot** — drive a session from your phone: send prompts, `/stop`, `/new`, `/status`, `/help`; chat-ID whitelist keeps strangers from spending your tokens
- **Peer & Swarm** — NECRO instances talk to each other: a fleet of containers can work on the same project in parallel (one builds, one develops a branch, one updates devices) — see below
- **Sub-agents** — `delegate` (hand a task to another model) and `consult` (bot-to-bot dialogue with follow-up questions)
- **Safety breaks for unattended runs** — per-turn wall-clock timeout and per-session turn cap, so a self-looping agent cannot run forever
- **SQLite session DB** with `recall` — keyword search over past sessions (last 7 days)

## The cool part: Swarm & Peers

<img alt="image" src="https://github.com/user-attachments/assets/d1dc83af-7d23-481e-aaac-63b0b96cc424" />

### Swarm (delegate / consult)

A swarm is a list of other models (each on its own inference server) that the main
model can use as **sub-agents**:

- `delegate` — hands a self-contained task to a swarm agent. It runs its own tool loop
  (bash, read_file, ...) in the same working directory and returns the result.
- `consult` — bot-to-bot consultation: the swarm agent may ask up to 3 follow-up
  questions, which you answer (it sees only your answers, not your full context).
  Good for second opinions and reviews.

Both tools are only registered when a `swarm` is configured in `necro.cfg`.

**Ping-pong protection (quick & dirty):** sub-agents do not receive the
`delegate`/`consult`/`peer` tools, so a sibling cannot hand tasks back to the master
while it is busy. Note: this is a tool-level guard, not a hard guarantee — the model
can still be prompted to do such things.

### Peer

When several NECRO sessions run against **different backends**, the sessions can
contact each other directly via the `peer` tool: a full agent turn is delegated to a
sibling NECRO instance (own context, own model — it runs the task in its own UI and
returns the final reply). If the peer is busy, the task is queued and runs after the
current turn.

**Multi-hop:** a peer turn may call another peer (chain, `peer_max_hops`,
default 1 = off — set 2+ for parent-child chains), so instances can network and
build multi-stage chains. The caller
is excluded from the candidate list (no bounce back), each hop is logged, and one
wall-clock budget covers the whole chain (the receiver's watchdog is capped by the
remaining time). `delegate`/`consult` are available inside peer turns by default
(`peer_turn_subagents`). The peer list shows each instance's busy status; without an
explicit target, idle peers are preferred.

The advantage: **identical prerequisites** — the peer runs the same harness with the
same tools, so it understands the environment without any translation layer.

**In practice — parallel containers on one codebase:** because each peer is a full
agent with its own context and its own terminal, several containers can work on the
*same project at the same time* without blocking each other:

- Container A builds an Android app from source (gradle, SDK, signing) while
- Container B develops the very same project source on a feature branch, and in the
  meantime
- Container C pushes system updates to a fleet of devices.

All three share the working directory, so they see the same files — but each runs its
own turn on its own model, in its own UI. One container can hand a sub-task to another
via `peer` (or fan out to `delegate`/`consult` inside that turn), making a small fleet
of containers behave like one team of specialists working in parallel.

> **Tip (Docker):** run peers in containers and share the discovery directory with a
> bind mount, so each container sees/finds easy the other instances:
>
> ```yaml
>     volumes:
>       - ./necro/peer:/tmp/necro
> ```

> **Multiple sessions on one client:** copy NECRO into its own folder per planned
> session. Each copy then has its own session DB and does not mix history with the
> others.

> **Safety warning:** peer/swarm is a young idea — it works, but it was not built with
> enormous care for safety (no auth on the peer API, quick & dirty ping-pong guard,
> ...). Do not run it unattended on a machine where that matters yet.

## Quickstart (local client)

Debian 13, project in `~/app`:
```
cd ~/app
.
├── data
└── modules
    ├── core
    ├── io
    └── ui
necro.py
necro.cfg
```

```bash
# Session 1
NECRO_BASE_URL=http://127.0.0.1:8080/v1 NECRO_MODEL=Qwen3.8-27B NECRO_REMOTE=1 ./necro.py

# Session 2
NECRO_BASE_URL=http://127.0.0.1:8080/v1 NECRO_MODEL=Ornith1.5-35B NECRO_REMOTE=1 ./necro.py
```

Environment variables always take precedence over `necro.cfg`.

## Configuration

`necro.cfg` (JSON) is read from the working directory, then from the script
directory. Example:

```json
{
  "baseurl": "http://127.0.0.1:8080/v1",
  "model": "Ornith1.5-35B",
  "swarm": [
    {
      "url": "http://127.0.0.1:8080/v1",
      "model": "Qwen_Qwen3.5-2B-Q4_K_M.gguf"
    },
    {
      "url": "http://127.0.0.1:8080/v1",
      "model": "gemma-4-E2B-it"
    }
  ],
  "reasoning": false,
  "show_thinking": false,
  "show_tools": false,
  "show_edits": true,
  "max_steps": 150,
  "max_turns": 200,
  "remote": true,
  "peer_host": "0.0.0.0",
  "peer_max_hops": 1,
  "peer_turn_subagents": true,
  "telegram_chats": "[chat-id]",
  "turn_timeout": 14400,
  "_doc_colors": {
    "_note": "role -> 0-255",
    "edit-old": 216,
    "agent": 106,
    "user": 216,
    "grey": 250,
    "dark": 240,
    "darker": 232,
    "red": 167,
    "panel-bg": 236,
    "header-fg": 255,
    "code-fg": 252,
    "edit-fg": 232,
    "telegram": 38,
    "edit-new": 106
  },
  "_doc_tool_emoji": {
    "_note": "tool -> emoji",
    "bash": "🦴",
    "edit_file": "✒️",
    "write_file": "✒️",
    "consult": "🕷️",
    "delegate": "🕷️",
    "recall": "🧠",
    "peer": "💀"
  }
}
```

## Documentation

Deeper dives live in `_docs/`:

| Doc | Content |
|-----|---------|
| [`startparams.md`](_docs/startparams.md) | All configuration sources: CLI flags, env vars, `necro.cfg` keys (single source of truth) |
| [`agent-lifecycle.md`](_docs/agent-lifecycle.md) | The agent loop: turns, steps, tool execution, sub-agents |
| [`peer-mechanism.md`](_docs/peer-mechanism.md) | How sibling instances discover each other and delegate turns |
| [`data-model.md`](_docs/data-model.md) | Session DB (SQLite) schema and pruning |
| [`tui-rendering.md`](_docs/tui-rendering.md) | Terminal UI: rendering, colors, keys, mouse |
| [`security.md`](_docs/security.md) | Guardrails for unattended operation |


## License

| License| NECROMANCER<br>(NECRO) |
| :--- | :---: |
| [GPL-3.0](LICENSE) (copyleft) — derivatives must stay open source.<br><br>Copyright (C) 2026 Daniel Neuhaus | <img width="200" alt="image" src="https://github.com/user-attachments/assets/22687e32-1f17-4c5c-be06-5b11b9d9d5ef" /> |

