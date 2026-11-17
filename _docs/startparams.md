# NECRO – Start Parameters and Variables

Single Source of Truth for all configuration sources: CLI flag, env vars,
`necro.cfg` keys. Priority: **env > cfg > default** (unless noted otherwise).

## Naming Rules

The names are mechanical, so you can guess one form from the other:

* **cfg keys**: `lower_snake_case`, no abbreviations (`base_url`, not `baseurl`;
  `ctx_fallback`, not `ctx_window`).
* **env vars**: `NECRO_` + the cfg key in upper case (`turn_timeout_s` ->
  `NECRO_TURN_TIMEOUT_S`). The env name never deviates from the cfg key.
* **units** are part of the name: `_s` = seconds, `_ms` = milliseconds,
  `_tokens` = tokens (`turn_timeout_s`, `max_output_tokens`).
* **scope** is part of the name: `turn_` = one user request, `session_` = the
  whole session, `step_` = one model request, `peer_` = the peer API.
* `_doc*` keys are comments and are ignored by the validation.

## CLI

| Flag | Effect |
|---|---|
| `--telegram` | Enable the Telegram client (long polling). Alternatively `NECRO_TELEGRAM=1`. Token via `NECRO_TELEGRAM_TOKEN` or cfg `telegram_token`. |
| `--colors` | Print the color roles (role, index, purpose) and exit — the help for cfg `colors`. |

## Environment Variables

| Variable | cfg key | Default | Meaning |
|---|---|---|---|
| `NECRO_BASE_URL` | `base_url` | `http://127.0.0.1:8080/v1` | OpenAI-compatible inference server URL |
| `NECRO_MODEL` | `model` | `gemma-4-E2B-it` | Main model (ID as delivered by `/models`) |
| `NECRO_TURN_MAX_STEPS` | `turn_max_steps` | `25` | Max model requests (steps) inside one turn |
| `NECRO_TURN_TIMEOUT_S` | `turn_timeout_s` | `3600` | Wall-clock timeout per turn in s (safety break; 0 = off) |
| `NECRO_SESSION_MAX_TURNS` | `session_max_turns` | `200` | Max turns per session (self-loop guard; 0 = off; `/new` resets) |
| `NECRO_CTX_FALLBACK` | `ctx_fallback` | `8192` | Fallback for the context window size; the real value comes from the inference server |
| `NECRO_STEP_TIMEOUT_S` | `step_timeout_s` | `300` | Socket timeout per inference step in s – a hung stream does not block the turn forever |
| `NECRO_PEER_HOST` | `peer_host` | `127.0.0.1` | Peer-API bind address (Docker: `0.0.0.0`) |
| `NECRO_PEER_PORT` | `peer_port` | `0` (ephemeral) | Peer-API port (Docker: fixed, exposed port) |
| `NECRO_PEER_TURN_TIMEOUT_S` | `peer_turn_timeout_s` | `600` | Max wall time of a peer turn in s (watchdog + HTTP waits; published in the discovery file, the PEER's value is used by callers) |
| `NECRO_PEER_DIR` | – | `/tmp/necro` | Discovery directory of the Peer-API (modules/io/api.py) |
| `NECRO_PEER_MAX_HOPS` | `peer_max_hops` | `1` | Max peer hops in one chain (1 = no multi-hop; 2+ = parent-child chains) |
| `NECRO_PEER_TURN_SUBAGENTS` | `peer_turn_subagents` | `false` | `delegate` available inside peer turns (`consult` always) |
| `NECRO_SUBAGENT_DELEGATE` | `subagent_delegate` | `false` | Nested `delegate` inside sub-agents (opt-in) |
| `NECRO_SUBAGENT_MAX_DEPTH` | `subagent_max_depth` | `1` | Max nesting levels for nested delegate (explicit cap) |
| `NECRO_MAX_OUTPUT_TOKENS` | `max_output_tokens` | `0` (off) | Cap output tokens per request when reasoning is OFF (best effort, server-specific) |
| `NECRO_SWARM` | – | – | Swarm list `model=url;model=url` (empty = off). Always activates the swarm, even without `remote`. |
| `NECRO_REMOTE` | `remote` | `false` | Remote mode: cfg swarm + Peer-API/peer tool. `1/true/on` = on, `0/off/false/""` = off. |
| `NECRO_PEER_API` | `peer_api` | `false` | Own switch for the Peer-API/peer tool only (like `NECRO_SWARM` for the swarm). Does NOT activate the cfg swarm list. |
| `NECRO_SWARM_CHECK` | – | `30` | Interval in s for the swarm reachability check (modules/io/swarm.py `check_loop`) |
| `NECRO_API_TOKEN` | – | – | Global Bearer token for the inference server (e.g. OpenRouter); overrides cfg `auth` |
| `NECRO_TELEGRAM` | – | – | `1` = enable Telegram (like `--telegram`) |
| `NECRO_TELEGRAM_TOKEN` | `telegram_token` | – | Bot token (only with `--telegram` / `NECRO_TELEGRAM=1`) |
| `NECRO_TELEGRAM_CHATS` | `telegram_chats` | – | Chat-ID whitelist, comma-separated (empty/absent = nobody, deny-all) |
| `NECRO_PROVIDER` | `provider` | – | Runner of the main endpoint (`openai`, `google`, `deepseek`, `bigmoe`) when the hostname gives no hint (see providers.md) |
| `NECRO_SESSION` | – | `run` | Session label for the turns-DB traces (data/necro.db) |
| `NECRO_DB` | – | `data/necro.db` (project dir) | Path of the turns-DB file (multiple instances can use separate DB files) |

## necro.cfg Keys (all optional, validation in modules/core/config.py)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `base_url` | str | see above | Inference server URL |
| `model` | str | see above | Main model |
| `swarm` | list | – | Delegable models `[{url?, model, provider?}]`; duplicate names need `name@host`; the main model is filtered out; only active with `remote=true` or `NECRO_SWARM` |
| `remote` | bool | `false` | Remote mode (cfg swarm + Peer-API/peer tool) |
| `peer_api` | bool | `false` | Peer-API/peer tool only (no cfg swarm) |
| `reasoning` | bool | auto | `true/false` = explicit reasoning level; omit the key = auto-detect at startup (probe) |
| `show_thinking` | bool | `true` | Startup default for the ^T toggle (thinking blocks) |
| `show_tools` | bool | `true` | Startup default for the ^O toggle (tool output) |
| `show_edits` | bool | `true` | Startup default for the ^X toggle (edit diffs) |
| `turn_max_steps` | int | `25` | Max model requests (steps) inside one turn |
| `session_max_turns` | int | `200` | Max turns per session (0 = off) |
| `ctx_fallback` | int | `8192` | Fallback context window (real size from the inference server) |
| `ctx_models` | dict | `{}` | Per-model context cap `{model: tokens}`; wins over the server window but never goes above it. Key = model name or a prefix of it; `-`, `.`, `_`, spaces and case are ignored (`Qwen3-8` hits `Qwen3.8-27B-Swift`). `"*"` = default for every model. Active entry shows as `(cap <key>)` in the status line and in `/status`. Keys starting with `_` are comments |
| `peer_host` / `peer_port` | str / int | see above | Peer-API bind |
| `peer_turn_timeout_s` | int | `600` | Max wall time of a peer turn in s |
| `peer_max_hops` | int | `1` | Max peer hops in one chain |
| `peer_turn_subagents` | bool | `false` | `delegate` inside peer turns (`consult` always) |
| `subagent_delegate` | bool | `false` | Nested `delegate` inside sub-agents |
| `subagent_max_depth` | int | `1` | Max nesting levels for nested delegate |
| `max_output_tokens` | int | `0` | Output token cap when reasoning is off (0 = off) |
| `step_timeout_s` | int | `300` | Socket timeout per model request (s) |
| `turn_timeout_s` | int | `3600` | Turn timeout (s; 0 = off) |
| `telegram_chats` | str | – | Chat-ID whitelist (comma-separated; empty = nobody, deny-all) |
| `telegram_token` | str | – | Bot token |
| `telegram_format` | str | `markdown` | TG format: `text` \| `html` \| `markdown` |
| `telegram_rich` | str | `auto` | Rich table detection (Bot API 10.1): `auto` \| `off` |
| `colors` | dict | see `./necro.py --colors` | role -> 256-color index 0-255, e.g. `{"accent": 208}`. The roles and their purpose: `./necro.py --colors` (no terminal needed: `python3 -m modules.core.palette`) |
| `tool_emoji` | dict | – | tool -> emoji string (unknown tools fall back to `›`) |
| `auth` | dict | – | optional per-server access tokens `{url or host:port: token}`, sent as Bearer auth |
| `provider` | str | – | runner of the main endpoint (`openai`, `google`, `deepseek`, `bigmoe`); only needed when the hostname gives no hint (see providers.md) |

Keys with the `_doc` prefix are comments (ignored by validation). Unknown
keys and wrong types are **warned at startup** (validation in
`modules/core/config.py`) — a typo in `base_url` no longer silently falls
back to the default.
