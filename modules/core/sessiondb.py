"""NECRO turns DB: one row per session, parallel-safe (busy_timeout), own turns only."""
import json, sqlite3, threading, time
from pathlib import Path
class SessionDB:
    """Own-session mirror of the agent context (all messages except system) in SQLite.
    Sessions older than 7 days are pruned at open.
    Parallel-safe: WAL + busy_timeout + autocommit with explicit transactions
    (legacy implicit transactions left the session INSERT open, holding the write
    lock, so a second instance crashed with 'database is locked')."""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._lock = threading.Lock()  # sqlite3 connections are not thread-safe for concurrent use
        # autocommit (isolation_level=None) + explicit 'with' transactions
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.execute("PRAGMA journal_mode = WAL")  # parallel readers + lock-free open (2nd instance no longer 'database is locked')
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # fast path - one query checks the whole schema (4 tables + 2 FTS
                # triggers); a complete schema skips the ~14 IF NOT EXISTS executes
                # (~2.5 ms per open, measured). Migration edge cases stay on the slow path.
                have = self.db.execute(
                    "SELECT (SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                    " AND name IN ('sessions', 'turns', 'traces', 'turns_fts'))"
                    " + (SELECT count(*) FROM sqlite_master WHERE type = 'trigger'"
                    " AND name IN ('turns_ai', 'turns_ad'))"
                    " + (SELECT count(*) FROM pragma_table_info('turns') WHERE name = 'session')").fetchone()[0]
                if have < 7:
                    self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL)")
                    if [r[1] for r in self.db.execute("PRAGMA table_info(turns)")] and \
                       "session" not in [r[1] for r in self.db.execute("PRAGMA table_info(turns)")]:
                        self.db.execute("DROP TABLE turns")  # migrate pre-session schema
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
                old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 7 * 86400))
                self.db.execute("DELETE FROM turns WHERE session IN (SELECT id FROM sessions WHERE ts < ?)", (old,))
                self.db.execute("DELETE FROM sessions WHERE ts < ?", (old,))
                self.db.execute("DELETE FROM traces WHERE ts < ?", (old,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        self.session_id = self.db.execute("INSERT INTO sessions (ts) VALUES (?)",
                                          (time.strftime("%Y-%m-%dT%H:%M:%S"),)).lastrowid
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
        original text instead of only the summary; the shared prefix is reused."""
        rows = [(m.get("role"), m.get("content"),
                 json.dumps(m.get("tool_calls") or [], ensure_ascii=False) if m.get("tool_calls") else None,
                 m.get("tool_call_id")) for m in messages[1:]]
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            # explicit transaction: autocommit mode would leave the session with an empty
            # turns table if a crash happens between the DELETE and the INSERTs (F4)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.db.execute("SELECT id, role, content FROM turns WHERE session = ? ORDER BY id",
                                      (self.session_id,)).fetchall()
                k = 0
                while (k < len(old) and k < len(rows) and old[k][1] == rows[k][0]
                       and (old[k][2] or None) == (rows[k][1] or None)):
                    k += 1
                for oid, _, _ in old[k:]:  # fell out of the context (compacted head): keep + mark
                    self.db.execute("UPDATE turns SET compacted = 1 WHERE id = ?", (oid,))
                if k < len(rows):
                    self.db.executemany("INSERT INTO turns (session, ts, role, content, tool_calls, tool_call_id) "
                                        "VALUES (?,?,?,?,?,?)",
                                        [(self.session_id, ts, r, c, tc, tci) for r, c, tc, tci in rows[k:]])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
