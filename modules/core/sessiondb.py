"""NECRO turns DB: one row per session, parallel-safe (busy_timeout), own turns only."""
import json, sqlite3, threading, time
from pathlib import Path

# Required schema objects for the fast open path (SSOT: the count below is
# derived from this tuple, so adding an entry cannot desync the check from the
# CREATE statements - the old hard-coded 'have < 7' broke silently).
_SCHEMA = (
    ("table", "sessions"), ("table", "turns"), ("table", "traces"),
    ("table", "turns_fts"),  # FTS5 index for recall()
    ("trigger", "turns_ai"), ("trigger", "turns_ad"),  # keep the index in sync
    ("column", "session"),  # column of turns; pre-session DBs lack it -> migration
)
PRUNE_DAYS = 7  # retention horizon of turns/traces (= recall() reach)
WAL_LIMIT = 4 * 1024 * 1024  # max WAL file size in bytes (checkpoint truncates above it)
class SessionDB:
    """Own-session mirror of the agent context (all messages except system) in SQLite.
    Sessions older than 7 days are pruned at open.
    Parallel-safe: WAL + busy_timeout + autocommit with explicit transactions
    (legacy implicit transactions left the session INSERT open, holding the write
    lock, so a second instance crashed with 'database is locked')."""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.closed = False  # daemon threads may still reach the DB during shutdown
        self._lock = threading.Lock()  # sqlite3 connections are not thread-safe for concurrent use
        # autocommit (isolation_level=None) + explicit 'with' transactions
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.execute("PRAGMA journal_mode = WAL")  # parallel readers + lock-free open (2nd instance no longer 'database is locked')
        # a long-running instance never shrinks the WAL on its own (it was 8.9 MB
        # against a 49 MB DB): cap the file, the data stays safe either way
        self.db.execute(f"PRAGMA journal_size_limit = {WAL_LIMIT}")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # fast path - one query checks the whole schema (see _SCHEMA); a
                # complete schema skips the ~14 IF NOT EXISTS executes (~2.5 ms per
                # open, measured). Migration edge cases stay on the slow path.
                if self._schema_count() < len(_SCHEMA):
                    self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL)")
                    if [r[1] for r in self.db.execute("PRAGMA table_info(turns)")] and \
                       "session" not in [r[1] for r in self.db.execute("PRAGMA table_info(turns)")]:
                        self._archive_legacy_turns()  # pre-session schema: keep the rows
                    self.db.execute("CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY, session INTEGER NOT NULL, "
                                    "ts TEXT NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_call_id TEXT, "
                                    "compacted INTEGER NOT NULL DEFAULT 0)")
                    # per-turn metrics trace (replaces the data/necro.jsonl file);
                    # full JSON blob per row so new metric fields need no schema change
                    self.db.execute("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, data TEXT NOT NULL)")
                    # FTS5 index over the turns table (external content) for cross-session
                    # keyword search (recall tool); triggers keep it in sync with sync()/pruning
                    had_fts = bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'turns_fts'").fetchone())
                    self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(content, content='turns', content_rowid='id')")
                    self.db.execute("CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN "
                                    "INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content); END")
                    self.db.execute("CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN "
                                    "INSERT INTO turns_fts(turns_fts, rowid, content) VALUES ('delete', old.id, old.content); END")
                    if not had_fts:
                        self.db.execute("INSERT INTO turns_fts(turns_fts) VALUES ('rebuild')")  # index existing rows
                self._prune_locked()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        self.session_id = self.db.execute("INSERT INTO sessions (ts) VALUES (?)",
                                          (time.strftime("%Y-%m-%dT%H:%M:%S"),)).lastrowid

    # ---- schema / lifecycle
    def _schema_count(self):
        """How many of the _SCHEMA objects exist (== len(_SCHEMA) means complete)."""
        parts, args = [], []
        for kind, name in _SCHEMA:
            if kind == "column":
                parts.append("(SELECT count(*) FROM pragma_table_info('turns') WHERE name = ?)")
            else:
                parts.append("(SELECT count(*) FROM sqlite_master WHERE type = ? AND name = ?)")
                args.append(kind)
            args.append(name)
        return self.db.execute("SELECT " + " + ".join(parts), args).fetchone()[0]

    def _archive_legacy_turns(self):
        """Pre-session schema: the rows carry no session id and cannot be
        reconciled - but dropping them lost real history (the old code did a bare
        DROP TABLE turns). Rename instead: the rows stay readable, the new turns
        table starts empty. The FTS triggers must go with it, otherwise the same
        trigger names would shadow the new ones and the index would stay empty."""
        n, base = 0, time.strftime("turns_legacy_%Y%m%d", time.localtime())
        while self._exists("table", f"{base}{'' if n == 0 else '_' + str(n)}"):
            n += 1
        legacy = f"{base}{'' if n == 0 else '_' + str(n)}"
        self.db.execute(f"ALTER TABLE turns RENAME TO {legacy}")
        for (name,) in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (legacy,)).fetchall():
            self.db.execute(f"DROP TRIGGER IF EXISTS {name}")

    def _exists(self, kind, name):
        return bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
                                    (kind, name)).fetchone())

    def _prune_locked(self):
        """Drop turns/traces older than PRUNE_DAYS (caller holds the transaction)."""
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - PRUNE_DAYS * 86400))
        self.db.execute("DELETE FROM turns WHERE session IN (SELECT id FROM sessions WHERE ts < ?)", (old,))
        self.db.execute("DELETE FROM sessions WHERE ts < ?", (old,))
        self.db.execute("DELETE FROM traces WHERE ts < ?", (old,))

    def prune(self):
        """Retention pass for long runners (the open-time prune never runs again
        otherwise). Cheap when nothing is old: the three DELETEs are index scans."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._prune_locked()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        """Release the connection (checkpoints the WAL). Idempotent, and safe to
        call from a signal/finally path: a failing close must not mask the exit."""
        with self._lock:
            self.closed = True
            try:
                self.db.close()
            except sqlite3.Error:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
    def recall(self, query, limit=10):
        """keyword search over PAST sessions (FTS5, last 7 days - the pruning
        horizon). OR semantics (any term hits, ranked by bm25) + prefix matching
        ('deadloc*' finds 'Deadlock'); if FTS yields nothing (stemming, special chars),
        a LIKE fallback over the same row set keeps recall usable. Returns formatted
        hits '[ts] role: text' (300 chars each); [] if none."""
        terms = [t.replace('"', '') for t in query.split() if t]
        if not terms:
            return []
        fts = " OR ".join('"%s"*' % t for t in terms)
        where = "(t.session != ? OR t.compacted = 1)"
        try:
            rows = self.db.execute(
                "SELECT s.ts, t.role, t.content FROM turns_fts f JOIN turns t ON t.id = f.rowid "
                "JOIN sessions s ON s.id = t.session WHERE turns_fts MATCH ? AND " + where +
                " ORDER BY bm25(turns_fts) LIMIT ?", (fts, self.session_id, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []  # FTS query syntax edge case: fall through to the LIKE search
        if not rows:  # FTS found nothing - LIKE over the same row set (OR, no ranking)
            like = " OR ".join(["t.content LIKE ? ESCAPE '\\'"] * len(terms))
            # escape LIKE wildcards so a literal '%'/'_' in the query is no glob
            args = ["%%%s%%" % t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for t in terms]
            rows = self.db.execute(
                "SELECT s.ts, t.role, t.content FROM turns t JOIN sessions s ON s.id = t.session "
                "WHERE " + like + " AND " + where + " ORDER BY s.ts DESC LIMIT ?",
                args + [self.session_id, limit]).fetchall()
        out = []
        for ts, role, content in rows:
            c = (content or "").strip().replace("\n", " ")
            if len(c) > 300:
                c = c[:300] + "…"
            out.append(f"[{ts}] {role}: {c}")
        return out
    def trace(self, **fields):
        """one DB row per turn (structured trace for A/B comparisons and
        self-improvement metrics); ts is indexed so measure.sh filters by range."""
        with self._lock:
            self.db.execute("INSERT INTO traces (ts, data) VALUES (?, ?)",
                            (fields["ts"], json.dumps(fields, ensure_ascii=False)))
    def sync(self, messages):
        """Mirror the active context (all messages except system) into the turns table.
        incremental - rows that leave the context (compact() replaced the head
        with a summary) are kept and marked compacted=1 so recall() still finds the
        original text instead of only the summary.
        Matching is the LONGEST SUFFIX of the new context whose identity keys equal
        the tail of the stored (non-compacted) rows:
        - append: the old rows are a prefix of the new context -> match_len == len(old),
          nothing is marked compacted, only the new tail is inserted. (A set-based
          match marked the old prefix rows 'gone' on EVERY append and re-inserted
          them - unbounded DB growth.)
        - compact: the head is replaced by a summary -> the surviving tail is
          recognised, the old head rows are marked compacted, only the summary
          (and any new tail) is inserted.
        - trim_tool_results(): a tool row keeps its identity (tool_call_id) -> the
          match is unaffected and the row is not duplicated.
        The DB keeps the ORIGINAL (untrimmed) content: recall searches history,
        the trimmed copy is only the live-context optimisation."""
        rows = [(m.get("role"), m.get("content"),
                 json.dumps(m.get("tool_calls") or [], ensure_ascii=False) if m.get("tool_calls") else None,
                 m.get("tool_call_id")) for m in messages[1:]]
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        def key(r):
            # identity of one context message (stable across trims/compacts).
            # assistant rows carry their tool_calls: every ReAct step emits an
            # assistant message with content=None + tool_calls, and without them
            # ALL of them collapse onto ("c", "assistant", None) -> only the
            # first one is ever inserted (data loss, DB != context).
            # tool rows keep tool_call_id (stable across trim_tool_results).
            if r[0] == "tool" and r[3]:
                return ("t", r[3])
            tc = r[2] if r[0] == "assistant" else None
            return ("c", r[0], r[1], tc)
        with self._lock:
            # explicit transaction: autocommit mode would leave the session with an empty
            # turns table if a crash happens between the DELETE and the INSERTs (F4)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # only NON-compacted rows may be recognised as the surviving tail:
                # a compacted row that reappears in the context is a NEW message
                # (re-inserted fresh, compacted=0), not the old one
                old = self.db.execute("SELECT id, role, content, tool_calls, tool_call_id FROM turns WHERE session = ? AND compacted = 0 ORDER BY id",
                                      (self.session_id,)).fetchall()
                old_keys = [key((r[1], r[2], r[3], r[4])) for r in old]
                new_keys = [key(r) for r in rows]
                # Two-phase match (the context only ever changes by APPEND or
                # COMPACT, so two cases cover it):
                # 1) append: the stored rows are a PREFIX of the new context ->
                #    nothing is compacted, only the appended tail is inserted.
                #    (Checked first: it is the common case and is unambiguous,
                #    even with duplicate content.)
                # 2) compact: the head was replaced by a summary -> the surviving
                #    tail (a suffix of the stored rows) is found in the new
                #    context; the old head rows before it are marked compacted,
                #    the summary (and any new tail) is inserted.
                # A pure set match re-inserts the old prefix on every append
                # (unbounded growth); a pure positional prefix match breaks with
                # duplicate content. This two-phase match handles both.
                is_prefix = len(new_keys) >= len(old_keys) and \
                    all(old_keys[i] == new_keys[i] for i in range(len(old_keys)))
                if is_prefix:
                    cut, new_rows = 0, rows[len(old_keys):]
                else:
                    match_k, match_pos = 0, 0
                    for k in range(min(len(old_keys), len(new_keys)), 0, -1):
                        blk = old_keys[-k:]
                        for pos in range(len(new_keys) - k + 1):
                            if new_keys[pos:pos + k] == blk:
                                match_k, match_pos = k, pos
                                break
                        if match_k:
                            break
                    cut = len(old_keys) - match_k  # stored rows before the match
                    new_rows = rows[:match_pos] + rows[match_pos + match_k:]
                if cut:
                    self.db.execute(f"UPDATE turns SET compacted = 1 WHERE id IN ({','.join('?' * cut)})",
                                    [r[0] for r in old[:cut]])
                # identity-set guard: a row whose identity already exists (e.g.
                # duplicate content) is not re-inserted
                old_id_set = set(old_keys)
                to_insert = [r for r in new_rows if key(r) not in old_id_set]
                if to_insert:
                    self.db.executemany("INSERT INTO turns (session, ts, role, content, tool_calls, tool_call_id) "
                                        "VALUES (?,?,?,?,?,?)",
                                        [(self.session_id, ts, r[0], r[1], r[2], r[3]) for r in to_insert])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
