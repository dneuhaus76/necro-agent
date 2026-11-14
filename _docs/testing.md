# NECRO – Testing

The test suite lives in `test/` (pytest). All tests are **fast unit tests
without a real inference server** — where HTTP is needed, the tests spin up
mock servers with `http.server` in a temp dir.

## Running

```bash
cd /workspace/necro
python3 -m pytest test/ -x -q
```

- The suite is designed to run in seconds, not minutes.
- `test_smoke.py` is the entry gate: every module must be importable, total
  runtime < 5 s.

## Conventions

- **One file per feature/bug** — the file name says what it covers
  (`test_peer_queue.py`, `test_tg_stranger.py`, ...).
- **No shared state:** tests use temp dirs and temp DB files
  (`NECRO_DB`, `NECRO_PEER_DIR` env overrides), so they never touch
  `data/necro.db` or a real `/tmp/necro`.
- **`.lock` files** next to some test files are `fcntl.flock` artifacts of
  the file-lock tests — ignore them, they are 0 bytes.
- Pure logic lives in parameterized modules (`swarm.py`, `context.py`,
  `tools.py`, `probe.py`, `render.py`) precisely so it is testable without an
  App instance or a server.

## Test areas

| Area | Files |
|---|---|
| Smoke / imports | `test_smoke.py` |
| Peer: discovery & status | `test_peer_discovery.py`, `test_peer_discovery_model.py`, `test_peer_status.py`, `test_peering.py`, `test_auth_token.py` |
| Peer: queue & busy | `test_peer_queue.py`, `test_peer_busy.py` |
| Peer: multi-hop & claims | `test_peer_multihop.py`, `test_peer_claims.py`, `test_zzz_budget.py` |
| Peer: context, dedup, abort | `test_peer_context.py`, `test_peer_dedup.py`, `test_peer_abort_reason.py` |
| Sub-agents | `test_subagent_delegate.py`, `test_consult_trim.py`, `test_swarm_check.py`, `test_swarm_offline.py` |
| Telegram | `test_tg_stranger.py`, `test_tg_busy.py`, `test_tg_rich.py` |
| Session DB / recall | `test_sessiondb.py`, `test_recall_render.py` |
| TUI | `test_cursor_busy.py`, `test_resize.py`, `test_scroll_cache.py`, `test_hist_nav.py`, `test_block_live.py`, `test_md_render.py`, `test_silent_turn.py` |
| Tools / files | `test_filelock.py`, `test_concurrent_edit.py` |
| Abort & retry | `test_abort_latency.py`, `test_ctrl_c.py`, `test_retry_window.py` |
| Guard rails | `test_prompt_guard.py`, `test_reply_guard.py`, `test_max_output_tokens.py`, `test_error_color.py` |
| Config / context | `test_necro_md_load.py`, `test_review_fixes.py`, `test_review_fixes2.py` |

`test_zzz_*` files run last on purpose (they need the full peer stack and are
the slowest).
