# NECRO – Core Tools

The five file/shell tools live in `modules/io/tools.py` (pure functions, no App
state); dispatch and result handling live in `modules/core/agent.py`
(`_TOOL_FNS`, `run_tool()`, `_run_one()`, `run_calls()`).

## Constants (SSOT in modules/io/tools.py)

| Constant | Value | Meaning |
|---|---|---|
| `BASH_TIMEOUT` | 120 s | Wall-time cap of the bash tool |
| `TOOL_OUT_CAP` | 8000 chars | Max chars of tool output sent to the model (bash + grep) |
| `CONTENT_CAP` | 20000 chars | Max chars of file/context content (read_file, result truncation, MD context load) |
| `GREP_MAX_COUNT` | 100 | Max matches per file for grep |
| `GREP_TIMEOUT` | 30 s | Wall-time cap of the grep tool |

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

## write_file

- Creates parent directories; a file component inside the path is rejected
  (`X is a file (not a directory)`).
- Serialized across processes and threads via `file_lock`
  (`modules/core/filelock.py`, `fcntl.flock` on a per-file `<name>.lock`).

## edit_file

- `old` must occur **exactly once** — 0 or 2+ occurrences is an error with the
  count.
- read + count + write run in ONE flock critical section, so concurrent edits
  of the same file (also from peer instances) cannot race.
- Returns a diff view (`-`/`+` lines) instead of the full new content.

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
