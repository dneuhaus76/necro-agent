# MANIFEST – NECRO Modules

Each line: `Module | Purpose | Public API | Dependencies`

Structure: `modules/core/` (pure logic), `modules/ui/` (rendering/sub-agent), `modules/io/` (outside world: peer/telegram/shell/swarm).

## core/ (pure logic, imported top-level by necro.py)

| Module | Purpose | Public API | Dependencies |
|--------|---------|------------|--------------|
| core.palette | Central color palette (single source of truth), cfg overrides, tool-emoji defaults | `PALETTE, EMOJI, ROLES, resolve(overrides)` | none |
| core.config | Reads necro.cfg (JSON), validates keys and types | `validate(cfg)` | core.palette |
| core.context | Context management (trimming, tool stats) | `trim_tool_results, under_after_trim, tool_stats, delegate_stats, tool_detail` | none |
| core.filelock | Cross-process file lock (flock on a sidecar `.lock` file) | `file_lock(path, timeout)` (contextmanager) | fcntl, contextlib |
| core.inference | HTTP connection to the inference server, SSE stream parsing | `open_conn, sse_events` | urllib.parse |
| core.probe | Inference server probe (model info, thinking detection) | `info_text, resolve, detect_thinking, fetch_model_info, default_info` | urllib.parse, concurrent.futures |
| core.sessiondb | SQLite session DB (WAL mode, 7-day pruning) | `SessionDB` | none |
| core.templates | Prompt template expansion (/review, /test, /plan) | `expand` | none |
| core.block | TUI log block (one user/agent/think/tool/info entry), state only | `Block` | none |
| core.turn | Agent turn execution (one turn = stream -> tool calls -> results), step/turn guard rails, turn trace; runs in its own thread | `TurnMixin` (mixin for App), `configure(**kw)` (values injected by necro.py) | core.inference (via app.agent), io.tools |
| core.agent | Agent loop (inference + tool execution): stream/complete/retry, sub-agent loop (delegate/consult), compact, tool dispatch | `Agent(app, base, step_timeout, tools, sub_tools, swarm, swarm_up, agents_md, agents_md_path)`, `ReplyError`, `parse_reply(raw)`, `content_of(msg)` (malformed-reply guards), `_kind(c)` (block kind: recall gets its own) | core.inference, io.tools, io.swarm, ui.subagent |

## ui/ (rendering + sub-agent prompt)

| Module | Purpose | Public API | Dependencies |
|--------|---------|------------|--------------|
| ui.render | Text/Markdown rendering, SGR color codes, word wrap | `cw, sw, clean, chunks, fit, sgr, emit, inline, wrap_segs, diff_view, md_lines, menu_line, set_palette, fg, bg, CHOCOLATE, GREY, DARK, RED, TELEGRAM, EMBLEM` | core.palette |
| ui.subagent | Sub-agent prompt and live progress | `subagent_prompt, progress_head` | none |
| ui.view | TUI view: block rendering (incl. the recall kind), screen drawing, input wrapping, incremental-draw cache | `ViewMixin` (mixin for App), `configure(**kw)`, `git_branch` | ui.render |
| ui.keys | Key handling: key dispatch, TAB completion, history navigation, CSI sequences, mouse mode, raw input feed | `KeyHandler` (mixin for App), `MOUSE_ON`, `MOUSE_OFF` | none (stdlib) |

## io/ (outside world: network, shell, messaging)

| Module | Purpose | Public API | Dependencies |
|--------|---------|------------|--------------|
| io.api | Peer discovery (HTTP, sibling instances; the discovery file publishes id/host/ip/port/token/model/url), multi-hop chain state (hops/budget in queue + worker), parent-child claims (N226) | `local_ip, peer_instances, peer_health, peer_busy, peer_prompt, peer_claim, peer_excluded_ids, PeerAPI(..., model, url)` + `announce(model, url)` (rewrites the discovery file, e.g. after /model), `OWN_ID` | http.server, pathlib |
| io.swarm | Swarm delegation (agent list, loop check, URL resolve) | `from_env, parse, resolve, agents_str, find, check_once, check_loop` | collections, urllib.parse, core.inference (token_for, lazy) |
| io.tg | Telegram bot integration (polling, queues) | `TelegramBot` | none |
| io.tools | Shell tools (bash, grep, read_file, write_file, edit_file); every long wait polls the turn's stop event in short slices | `run_bash, run_grep, read_file, write_file, edit_file, stop_hit, wait_slices, kill_group, cap_text, BASH_TIMEOUT, GREP_TIMEOUT, TOOL_OUT_CAP, CONTENT_CAP, GREP_MAX_COUNT` | modules.ui.render, modules.core.filelock |

## Import Boundaries

- **Core (necro.py)** must **never** import `modules.io.tg` or `modules.io.api` top-level.
- Both are imported **lazily**: `modules.io.api` in `peer_refresh()` and `main()`, `modules.io.tg` in `main()`.
- `modules.io.tools` imports `modules.ui.render` (SGR colors in tool output).
- `modules.core.agent` imports `modules.io.tools`/`modules.io.swarm` top-level and `modules.io.api` lazily (peer tool) – exception to the layer rule: the agent loop is the only core component that calls outside-world tools.
- Layer direction: core <- ui <- io (io may import ui, never the other way around).
