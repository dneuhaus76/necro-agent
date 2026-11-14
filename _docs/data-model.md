# NECRO – Data Model (Session DB)

NECRO's persistent store is a single SQLite file (`data/necro.db`, default;
movable via `NECRO_DB`). It holds sessions, turns and turn metrics. The layer
lives in `modules/core/sessiondb.py`.

## Files

| File | Purpose |
|---|---|
| `data/necro.db` | Main file (sessions, turns, traces) |
| `data/necro.db-wal` | Write-ahead log (WAL mode) |
| `data/necro.db-shm` | Shared-memory segment (WAL mode) |

The `-wal`/`-shm` side files deliberately live in `data/` so the project root
is not cluttered.

## Schema

```sql
sessions (id INTEGER PK, ts TEXT NOT NULL)          -- one row per start/session
turns  (id INTEGER PK, session INTEGER NOT NULL,    -- turn rows (mirror of the context)
        ts TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT, tool_calls TEXT, tool_call_id TEXT, compacted INTEGER DEFAULT 0)
traces (id INTEGER PK, ts TEXT NOT NULL, data TEXT NOT NULL)  -- per-turn metrics (JSON)
turns_fts (VIRTUAL TABLE fts5 ...)                  -- full-text index for recall()
```

### Triggers (FTS Sync)

`turns_ai` (AFTER INSERT) and `turns_ad` (AFTER DELETE) keep the FTS5 index
(`turns_fts`) in sync with the `turns` table – the FTS table is an
"external content" table that stores no data of its own.

## Parallel Safety

WAL + `busy_timeout = 5000` + autocommit (`isolation_level=None`) with
explicit `BEGIN IMMEDIATE`/`COMMIT` transactions. Legacy implicit
transactions left the `INSERT session` open and held the write lock – a
second instance then got "database is locked". Fix: every change in an
explicit transaction, connections protected across threads via `_lock`
(SQLite C connections are not thread-safe).

File writes (`write_file`/`edit_file`) are serialized across PROCESSES via
`modules/core/filelock.py` (`fcntl.flock` on a per-file `<name>.lock` file,
0 bytes, stays in place). The lock file is used instead of the data file
because `os.replace` swaps the data file's inode. Locks release on FD close
(also on crash) – no stale-lock problem. Works across the bind mounts here
(same underlying volume); not portable to NFSv3 without flock support.
(See tools.md for the atomic write + edit critical section.)

## 7-Day Pruning

On open, turns/sessions/traces older than 7 days are deleted. This is the
horizon for `recall()` – older sessions are no longer searchable.

## Core Methods (`SessionDB`)

| Method | Purpose |
|---|---|
| `__init__(path)` | Opens/initializes the DB, prunes old entries, creates `session_id` |
| `sync(messages)` | Mirrors the active context (all except system) into `turns`; incremental – trimmed head lines are kept as `compacted=1` |
| `recall(query, limit=10)` | Full-text search over past sessions (FTS5, BM25 ranking + LIKE fallback); OR semantics, prefix matching |
| `trace(**fields)` | One DB row per turn with structured metrics (JSON) |

### `sync()` – Incremental Mirror

`sync()` mirrors the messages (`messages[1:]`, i.e. without the system line):

- The **common prefix** is reused (no needless delete/insert).
- **Dropped lines** (replaced by `compact()`) are kept and marked
  `compacted=1` – `recall()` finds the original text, not just the summary.
- The explicit transaction prevents an empty `turns` table after a crash
  between `DELETE` and `INSERT`.

### `recall()` – Search

1. **FTS5** over `turns_fts` (JOIN with `sessions`/`turns`), BM25 ranking,
   OR semantics (`term1 OR term2`), prefix matching (`deadloc*`).
2. **LIKE fallback** if FTS finds nothing (stemming/special-char edge cases).
3. The current session is excluded (`session != session_id`), except
   compacted head lines (`compacted=1`) – so old, replaced content is found
   too.
4. Returns `[ts] role: <text>` (300 chars each, lines joined into one string).

## Turn Metrics (`trace`)

Per turn, `App.turn()` (necro.py) writes one row into `traces` with:

- **Time/identity:** `ts`, `model`, `session`
- **Flow:** `steps`, `tools` (list of called tools), `error`
- **Quality:** `tool_fails`, `tool_chars`
- **Speed:** `tools_ms`, `tool_detail` (per-tool breakdown), `parallel` (steps/wall_ms/sum_ms)
- **Delegation:** `delegates` (fallbacks + sub-agent steps)
- **Tokens:** `prompt_tokens`, `completion_tokens`, `latency_ms`

This data serves A/B comparisons and the self-improvement loop. `ts` is
indexed for time series.
