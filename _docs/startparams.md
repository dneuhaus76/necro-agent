# NECRO – Start Parameters and Variables

Single Source of Truth for all configuration sources: CLI flag, env vars,
`necro.cfg` keys. Priority: **env > cfg > default** (unless noted otherwise).

## CLI

| Flag | Effect |
|---|---|
| `--telegram` | Enable the Telegram client (long polling). Alternatively `NECRO_TELEGRAM=1`. Token via `NECRO_TELEGRAM_TOKEN` or cfg `telegram_token`. |

## Environment Variables

| Variable | cfg key | Default | Meaning |
|---|---|---|---|
| `NECRO_BASE_URL` | `baseurl` | `http://127.0.0.1:8080/v1` | OpenAI-compatible inference server URL |
| `NECRO_MODEL` | `model` | `Qwen3.8-27B` | Main model (ID as delivered by `/models`) |
| `NECRO_MAX_STEPS` | `max_steps` | `25` | Max agent steps per turn |
| `NECRO_TURN_TIMEOUT` | `turn_timeout` | `3600` | Wall-clock timeout per turn in s (safety break; 0 = off) |
| `NECRO_MAX_TURNS` | `max_turns` | `200` | Max turns per session (self-loop guard; 0 = off; `/new` resets) |
| `NECRO_CTX_WINDOW` | `ctx_window` | `8192` | Fallback for the context window size; the real value comes from the inference server |
| `NECRO_STEP_TIMEOUT` | `step_timeout` | `300` | Socket timeout per inference step in s – a hung stream does not block the turn forever |
| `NECRO_PEER_HOST` | `peer_host` | `127.0.0.1` | Peer-API bind address (Docker: `0.0.0.0`) |
| `NECRO_PEER_PORT` | `peer_port` | `0` (ephemeral) | Peer-API port (Docker: fixed, exposed port) |
| `NECRO_PEER_TURN_TIMEOUT` | `peer_turn_timeout` | `600` | Max wall time of a peer turn in s (watchdog + HTTP waits; published in the discovery file, the PEER's value is used by callers) |
| `NECRO_PEER_DIR` | – | `/tmp/necro` | Discovery directory of the Peer-API (modules/io/api.py) |
| `NECRO_PEER_MAX_HOPS` | `peer_max_hops` | `1` | Max peer hops in one chain (1 = no multi-hop; 2+ = parent-child chains) |
| `NECRO_PEER_TURN_SUBAGENTS` | `peer_turn_subagents` | `true` | `delegate`/`consult` available inside peer turns |
| `NECRO_SUBAGENT_DELEGATE` | `subagent_delegate` | `false` | Nested `delegate` inside sub-agents (opt-in) |
| `NECRO_SUBAGENT_MAX_DEPTH` | `subagent_max_depth` | `1` | Max nesting levels for nested delegate (explicit cap) |
| `NECRO_MAX_OUTPUT_TOKENS` | `max_output_tokens` | `0` (off) | Cap output tokens per request when reasoning is OFF (best effort, server-specific) |
| `NECRO_SWARM` | – | – | Swarm list `model=url;model=url` (empty = off). Always activates the swarm, even without `remote`. |
| `NECRO_REMOTE` | `remote` | `false` | Remote mode: cfg swarm + Peer-API/peer tool. `1/true/on` = on, `0/off/false/""` = off. |
| `NECRO_PEERING` | `peering` | `false` | Own switch for the Peer-API/peer tool only (like `NECRO_SWARM` for the swarm). Does NOT activate the cfg swarm list. |
| `NECRO_SWARM_CHECK` | – | `30` | Interval in s for the swarm reachability check (modules/io/swarm.py `check_loop`) |
| `NECRO_API_TOKEN` | – | – | Global Bearer token for the inference server (e.g. OpenRouter); overrides cfg `auth` |
| `NECRO_TELEGRAM` | – | – | `1` = enable Telegram (like `--telegram`) |
| `NECRO_TELEGRAM_TOKEN` | `telegram_token` | – | Bot token (only with `--telegram` / `NECRO_TELEGRAM=1`) |
| `NECRO_TELEGRAM_CHATS` | `telegram_chats` | – | Chat-ID whitelist, comma-separated (empty/absent = all chats) |
| `NECRO_SESSION` | – | `run` | Session label for the turns-DB traces (data/necro.db) |
| `NECRO_DB` | – | `data/necro.db` (project dir) | Path of the turns-DB file (multiple instances can use separate DB files) |

## necro.cfg Keys (all optional, validation in modules/core/config.py)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `baseurl` | str | see above | Inference server URL |
| `model` | str | see above | Main model |
| `swarm` | list | – | Delegable models `[{url?, model}]`; duplicate names need `name@host`; the main model is filtered out; only active with `remote=true` or `NECRO_SWARM` |
| `remote` | bool | `false` | Remote mode (cfg swarm + Peer-API/peer tool) |
| `peering` | bool | `false` | Peer-API/peer tool only (no cfg swarm) |
| `reasoning` | bool | auto | `true/false` = explicit reasoning level; omit the key = auto-detect at startup (probe) |
| `show_thinking` | bool | `true` | Startup default for the ^T toggle (thinking blocks) |
| `show_tools` | bool | `true` | Startup default for the ^O toggle (tool output) |
| `show_edits` | bool | `true` | Startup default for the ^X toggle (edit diffs) |
| `max_steps` | int | `25` | Max agent steps per turn |
| `max_turns` | int | `200` | Max turns per session (0 = off) |
| `ctx_window` | int | `8192` | Fallback context window (real size from the inference server) |
| `peer_host` / `peer_port` | str / int | see above | Peer-API bind |
| `peer_turn_timeout` | int | `600` | Max wall time of a peer turn in s |
| `peer_max_hops` | int | `1` | Max peer hops in one chain |
| `peer_turn_subagents` | bool | `true` | `delegate`/`consult` inside peer turns |
| `subagent_delegate` | bool | `false` | Nested `delegate` inside sub-agents |
| `subagent_max_depth` | int | `1` | Max nesting levels for nested delegate |
| `max_output_tokens` | int | `0` | Output token cap when reasoning is off (0 = off) |
| `step_timeout` | int | `300` | Socket timeout per step (s) |
| `turn_timeout` | int | `3600` | Turn timeout (s; 0 = off) |
| `telegram_chats` | str | – | Chat-ID whitelist (comma-separated) |
| `telegram_token` | str | – | Bot token |
| `telegram_format` | str | `markdown` | TG format: `text` \| `html` \| `markdown` |
| `telegram_rich` | str | `auto` | Rich table detection (Bot API 10.1): `auto` \| `off` |
| `colors` | dict | – | role -> 0-255 (roles from modules/core/palette.py `ROLES`) |
| `tool_emoji` | dict | – | tool -> emoji string (unknown tools fall back to `›`) |
| `auth` | dict | – | optional per-server access tokens `{url or host:port: token}`, sent as Bearer auth |

Keys with the `_doc` prefix are comments (ignored by validation). Unknown
keys and wrong types are **warned at startup** (validation in
`modules/core/config.py`) — a typo in `baseurl` no longer silently falls
back to the default.
