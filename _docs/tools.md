# NECRO – Core Tools

The file/shell/image tools live in `modules/io/tools.py` (pure functions, no
App state); dispatch and result handling live in `modules/core/agent.py`
(`_TOOL_FNS`, `run_tool()`, `_run_one()`, `run_calls()`).

## Constants (SSOT in modules/io/tools.py)

| Constant | Value | Meaning |
|---|---|---|
| `BASH_TIMEOUT` | 120 s | Wall-time cap of the bash tool |
| `TOOL_OUT_CAP` | 8000 chars | Max chars of tool output sent to the model (bash + grep) |
| `CONTENT_CAP` | 20000 chars | Max chars of file/context content (read_file, result truncation, MD context load) |
| `GREP_MAX_COUNT` | 100 | Max matches per file for grep |
| `GREP_TIMEOUT` | 30 s | Wall-time cap of the grep tool |
| `LOOK_MAX_BYTES` | 8 MB | Max image size of the look tool |

## bash

- `subprocess.Popen(shell=True, start_new_session=True)` — own process group.
- **Live streaming:** a pump thread reads stdout lines and appends them to the
  tool block in the UI (`on_line`); the turn loop skips the double append
  (`blk.live`).
- **Abort in short slices:** the join runs in 0.2 s slices polling `stop`, so
  `^c` kills a long command immediately instead of waiting for the timeout.
- **Timeout/abort:** `os.killpg(SIGKILL)` on the whole process group (a shell
  wrapper would otherwise survive its children and keep the pipe open).
- A background grandchild (`sleep 100 &`) that keeps the stdout pipe open after
  the shell exited is killed the same way (bounded join, then the normal
  timeout path).
- stderr is merged into stdout; the model sees
  `<output>\n[timeout after 120 s]` / `[aborted]` + `[exit N]`.
- Output is capped at `TOOL_OUT_CAP` with a `…[truncated]` marker.
- Each call starts in the process working dir (`cd` does not persist); the
  harness re-checks the cwd afterwards (bash may have changed it).

## grep

- `rg -n --max-count 100` (ripgrep), fallback `grep -rnE` when `rg` is missing.
- 30 s wall cap, output capped at `TOOL_OUT_CAP`.
- The wait polls `stop` in 20 ms slices (`wait_slices`) — an abort does not
  hang until the timeout.
- Returns `file:line: text` (directory) or `line: text` (single file).

## read_file

- 1-based `offset`/`limit`; `offset` beyond EOF is a precise error.
- **Fast path** (`offset=1`, no limit): only the first `CONTENT_CAP` chars are
  read from disk (`f.read(CONTENT_CAP)`) — a huge file is not fully loaded.
  The cut lands on a line boundary and the continuation marker points to the
  NEXT full line: `…[truncated, N bytes total - continue with offset X]`.
- A file of exactly `CONTENT_CAP` chars is NOT reported truncated (an endless
  re-read of the same content would result).
- **Range path** (`offset`/`limit`): read line by line, only the requested lines
  plus ONE probe line ("is there more?") are touched. `offset`/`limit` come from
  the model, so both are bounded: `limit` is capped by `CONTENT_CAP` chars and
  the file is never fully loaded (a 300 MB log with `limit=1` costs ~11 MB RSS,
  the previous `read_text()` version cost 611 MB).
- The header names the range and whether more follows:
  `[lines 2-2+, more follows]` vs `[lines 3-3]`.

## write_file

- Creates parent directories; a file component inside the path is rejected
  (`X is a file (not a directory)`).
- Serialized across processes and threads via `file_lock`
  (`modules/core/filelock.py`, `fcntl.flock` on a per-file `<name>.lock`).
- Written via `atomic_write` (tmp file + `os.replace`): the tmp name is unique
  per thread, the mode of an existing target is preserved, and a failed write
  removes its tmp file. Same helper as the peer discovery file (there with
  `mode=0o600`).

## edit_file

- `old` must occur **exactly once** — 0 or 2+ occurrences is an error with the
  count.
- read + count + write run in ONE flock critical section, so concurrent edits
  of the same file (also from peer instances) cannot race.
- Returns a diff view (`-`/`+` lines) instead of the full new content.

## look

`run_look(path)` returns `(text_line, base64, mime)`; on any problem
`(error string, None, None)`.

- **Type by magic bytes, not by suffix:** JPEG `ff d8 ff`, PNG
  `89 50 4e 47`, WebP `RIFF` at 0 + `WEBP` at 8. GIF/BMP are rejected with a
  clear error (no mmproj support).
- **Guards before the read:** directory, missing file, empty file,
  `LOOK_MAX_BYTES`. The size check comes first, so a 200 MB file is never read
  just to be rejected.
- The **tool result is a plain string** (`image: /p/a.png (png, 140 KB)`) —
  `turn.py`, `context.py` and `sessiondb.py` never see a content list.
- The image rides in an **extra user message** with an `image_url` part
  (`data:<mime>;base64,...`), injected by `Agent.stream()` right before the
  request and replaced by `[image: <path>]` right after it — also on abort
  (`_push_images` / `_settle_images` in a `try/finally`). So exactly ONE
  request pays the base64 prefill, and the session DB only ever stores the
  path. Wants the model the image again, it calls `look` again.
- Pending images are a **list**, not one slot: two parallel `look` calls in
  one step both survive.
- Available in `TOOLS`, `SUB_TOOLS` (a `delegate_fallback` runs on the local
  model with mmproj) and in peer turns.

## Atomic writes (crash safety)

`_atomic_write()` (both write_file and edit_file):

1. Write the full new content to `<name>.<pid>-<tid>.tmp` in the same
   directory (unique per thread — a leftover tmp of a dead thread is not
   mistaken for this write's temp file).
2. If the target exists, copy its mode onto the tmp file (a fresh tmp would
   reset permissions to the umask default, e.g. lose `u+x`).
3. `os.replace(tmp, target)` — atomic on the same filesystem: readers see
   either the old or the new file, never a partial one.

## Result truncation (agent._run_one)

Every tool result longer than `CONTENT_CAP + 100` is cut to the first half +
`…[truncated]…` + the last half, so the model keeps both ends of a large
output. The +100 tolerance covers tool-specific markers (read_file
continuation).

## Error convention

All harness errors are returned as tool results starting with `error: ...`
(invalid JSON args, unknown tool, missing argument, file errors). The model
sees exactly this prefix; the turn trace counts `error:` results as
`tool_fails` (quality signal).

## Parallel execution (`run_calls`)

- A single tool call runs on the calling thread (no pool overhead).
- Multiple calls of one step run in parallel in a `ThreadPoolExecutor`;
  workers = 4 base + 1 per online swarm agent, cap 8 (see swarm.md).
- Tool blocks are created first (in call order) so the UI shows them in
  order; only the execution is parallel.
- Results come back in call order, so the tool messages stay aligned with the
  assistant message's `tool_calls`.
- Wall time vs. sum of per-call times lands in the turn trace as a
  parallelism metric.
