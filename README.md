# NECROMANCER (NECRO)

*A self-hosted LLM agent for your terminal — it finds its siblings and works as a team. Zero dependencies, Python 3 stdlib only.*

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-green.svg)](https://www.python.org/)
[![No dependencies](https://img.shields.io/badge/deps-none-brightgreen.svg)](#quickstart)

**NECRO** is a small, self-contained agent harness for the terminal. It is inspired by
[pi-agent](https://github.com/earendil-works/pi) — an amazing product.

One script. No `pip install`, no virtualenv, no 40-package dependency tree. Point it at
any OpenAI-compatible inference server and you have a working agent — and when you run
a second one, they **find each other** and start working in parallel.

## Quickstart (30 seconds)

Debian 13, project in `~/app`:

```
cd ~/app
.
├── data
├── modules
│   ├── core
│   ├── io
│   └── ui
├── necro.py
└── necro.cfg
```

```bash
# Session 1
NECRO_BASE_URL=http://127.0.0.1:8080/v1 NECRO_MODEL=Qwen3.8-27B NECRO_REMOTE=1 ./necro.py

# Session 2 — same project, different model
NECRO_BASE_URL=http://127.0.0.1:8080/v1 NECRO_MODEL=Ornith1.5-35B NECRO_REMOTE=1 ./necro.py
```

That's it. Environment variables always take precedence over `necro.cfg`. No build step,
no package manager, nothing to install — it is the Python standard library.

## The cool part: Peers & Swarm

<img alt="image" src="https://github.com/user-attachments/assets/d1dc83af-7d23-481e-aaac-63b0b96cc424" />

### Peer — agents that find each other

This is the part that makes NECRO more than "another terminal agent." When several NECRO
sessions run against **different backends**, they discover each other automatically and
can hand a full agent turn to a sibling via the `peer` tool: own context, own model, own
terminal — the sibling runs the task in its own UI and returns the final reply.

**No config, no endpoints, no auth setup.** Share a folder (a bind mount), and the
instances appear to each other:

```yaml
    volumes:
      - ./necro/peer:/tmp/necro
```

**One-liner for a "restricted" peer container** (e.g. an untrusted model from
OpenRouter): a single `ssh` + `docker compose run` starts a disposable NECRO peer
against a hosted API — the token is injected from the host shell, the container
is gone after `--rm`:

```bash
ssh -t [user]@[dockerhost] 'docker compose -f /home/docker/necro-agent/docker-compose_untrusted.yml \
  run --rm necro-agent bash -c "NECRO_BASE_URL=https://openrouter.ai/api/v1 \
  NECRO_MODEL=nvidia/nemotron-3.5-lightning:free NECRO_API_TOKEN=${OPENROUTER_TOKEN} /app/necro.py"'
```

**In practice — parallel containers on one codebase:** because each peer is a full agent
with its own context and its own terminal, several containers can work on the *same
project at the same time* without blocking each other:

- Container A builds an Android app from source (gradle, SDK, signing) while
- Container B develops the very same project source on a feature branch, and in the
  meantime
- Container C pushes system updates to a fleet of devices.

All three share the working directory, so they see the same files — but each runs its own
turn on its own model, in its own UI. One container can hand a sub-task to another via
`peer` (or fan out to `delegate`/`consult` inside that turn), making a small fleet of
containers behave like one team of specialists working in parallel.

The advantage: **identical prerequisites** — the peer runs the same harness with the same
tools, so it understands the environment without any translation layer.

**Multi-hop:** a peer turn may call another peer (chain, `peer_max_hops`, default 1 = off
— set 2+ for parent-child chains), so instances can network and build multi-stage chains.
The caller is excluded from the candidate list (no bounce back), each hop is logged, and
one wall-clock budget covers the whole chain (the receiver's watchdog is capped by the
remaining time). `delegate`/`consult` are available inside peer turns by default
(`peer_turn_subagents`). The peer list shows each instance's busy status; without an
explicit target, idle peers are preferred.

> **Multiple sessions on one client:** copy NECRO into its own folder per planned session.
> Each copy then has its own session DB and does not mix history with the others.

> **Safety warning:** peer/swarm is a young idea — it works, but it was not built with
> enormous care for safety (no auth on the peer API, quick & dirty ping-pong guard, ...).
> Do not run it unattended on a machine where that matters yet.

### Swarm (delegate / consult)

A swarm is a list of other models (each on its own inference server) that the main model
can use as **sub-agents**:

- `delegate` — hands a self-contained task to a swarm agent. It runs its own tool loop
  (bash, read_file, ...) in the same working directory and returns the result.
- `consult` — bot-to-bot consultation: the swarm agent may ask up to 3 follow-up
  questions, which you answer (it sees only your answers, not your full context).
  Good for second opinions and reviews.

Both tools are only registered when a `swarm` is configured in `necro.cfg`.

**Ping-pong protection (quick & dirty):** sub-agents do not receive the
`delegate`/`consult`/`peer` tools, so a sibling cannot hand tasks back to the master
while it is busy. Note: this is a tool-level guard, not a hard guarantee — the model can
still be prompted to do such things.

**Nested delegate (opt-in):** `subagent_delegate` (default `false`) re-adds `delegate` to
sub-agents for parallel decomposition. Nesting is explicitly capped by
`subagent_max_depth` (default 1 = direct delegate only, no nesting — the cap, not the
10-step loop budget, is the limit). `consult` stays off inside sub-agents. Nested calls
are counted in the turn trace (`delegates.sub_delegates`).

**"Think less" (opt-in):** `max_output_tokens` (default `0` = off) caps the output tokens
of every request **when reasoning is off** — a best-effort way to keep answers short
without changing the model (reasoning-token counting is server-specific; the reliable
alternative is a fast/non-thinking model).

## Features

- **Tiny harness** — one main script plus a few modules, Python 3 standard library only, no dependencies to install
- **Automatic context size** — NECRO queries the inference server for the model's current context window (`--ctx-size`) at startup; no manual `ctx_window` needed
- **Telegram bot** — drive a session from your phone: send prompts, `/stop`, `/new`, `/status`, `/help`; chat-ID whitelist keeps strangers from spending your tokens
- **Peer & Swarm** — NECRO instances talk to each other: a fleet of containers can work on the same project in parallel — see above
- **Sub-agents** — `delegate` (hand a task to another model) and `consult` (bot-to-bot dialogue with follow-up questions)
- **Safety breaks for unattended runs** — per-turn wall-clock timeout and per-session turn cap, so a self-looping agent cannot run forever
- **SQLite session DB** with `recall` — keyword search over past sessions (last 7 days)

## Configuration

`necro.cfg` (JSON) is read from the working directory, then from the script directory.

_Enter the correct baseurl of the hosts if they are remote _

Example:

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
  "reasoning": true,
  "show_thinking": false,
  "show_tools": false,
  "show_edits": true,
  "max_steps": 150,
  "max_turns": 200,
  "remote": true,
  "peer_host": "0.0.0.0",
  "peer_turn_timeout": 3600,
  "telegram_chats": "[chat-id]",
  "turn_timeout": 14400,
  "step_timeout": 900,
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
    "peer": "💀",
    "thinking": "🧠",
    "grep": "🔍"
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
| [`swarm.md`](_docs/swarm.md) | Swarm sub-agents: delegate/consult, name resolution, availability checks |
| [`telegram.md`](_docs/telegram.md) | Telegram bridge: long polling, whitelist, commands |
| [`tools.md`](_docs/tools.md) | The five core tools: bash, grep, read/write, output caps |
| [`testing.md`](_docs/testing.md) | Test suite: layout, conventions, how to run |
| [`data-model.md`](_docs/data-model.md) | Session DB (SQLite) schema and pruning |
| [`tui-rendering.md`](_docs/tui-rendering.md) | Terminal UI: rendering, colors, keys |
| [`security.md`](_docs/security.md) | Guardrails for unattended operation |

## License

| License| NECROMANCER<br>(NECRO) |
| :--- | :---: |
| [GPL-3.0](LICENSE) (copyleft) — derivatives must stay open source.<br><br>Copyright (C) 2026 Daniel Neuhaus | <img width="200" alt="image" src="https://github.com/user-attachments/assets/22687e32-1f17-4c5c-be06-5b11b9d9d5ef" /> |
