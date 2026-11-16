"""Direct tests for modules/core/sessiondb.py (the module had zero direct tests).
Covers the identity-based sync(): incremental appends, assistant tool-call
messages (content=None + tool_calls - the data-loss bug where all of them
collapsed onto one identity), duplicate content, compact simulation
(rows leaving the context are marked compacted, not deleted), and recall()."""
import json

from modules.core.sessiondb import SessionDB, _SCHEMA, PRUNE_DAYS


def _db(tmp_path):
    return SessionDB(str(tmp_path / "necro.db"))


def _rows(db):
    return db.db.execute(
        "SELECT role, content, tool_calls, tool_call_id, compacted FROM turns "
        "WHERE session = ? ORDER BY id", (db.session_id,)).fetchall()


def test_incremental_sync_tool_call_steps(tmp_path):
    """The reported bug: a ReAct turn emits assistant messages with
    content=None + tool_calls. Syncing step by step must keep EVERY message -
    with the old identity (role, content) all assistant tool-call steps
    collapsed onto ('c', 'assistant', None) and only the first was inserted."""
    db = _db(tmp_path)
    msgs = [
        {"role": "system", "content": "sys"},  # skipped by sync()
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out1"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "out2"},
        {"role": "assistant", "content": "final answer"},
    ]
    for i in range(1, len(msgs)):  # sync after every message (incremental)
        db.sync(msgs[:i + 1])
    rows = _rows(db)
    # 6 non-system messages -> 6 rows (was 4 with the collapsing identity)
    assert len(rows) == 6, f"expected 6 rows, got {len(rows)}: {rows}"
    roles = [r[0] for r in rows]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]
    # both assistant tool-call steps are stored with their tool_calls
    tc_rows = [r for r in rows if r[2]]
    assert len(tc_rows) == 2
    assert json.loads(tc_rows[0][2])[0]["id"] == "c1"
    assert json.loads(tc_rows[1][2])[0]["id"] == "c2"
    # tool rows keep their tool_call_id
    assert [r[3] for r in rows if r[0] == "tool"] == ["c1", "c2"]


def test_duplicate_content_kept_separately(tmp_path):
    """Two identical user messages are separate turns - both must be stored
    (the identity includes position via the shared-tail match, not just content)."""
    db = _db(tmp_path)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "same question"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "same question"},
        {"role": "assistant", "content": "answer 2"},
    ]
    db.sync(msgs)
    rows = _rows(db)
    assert len(rows) == 4, f"expected 4 rows, got {len(rows)}: {rows}"
    assert sum(1 for r in rows if r[1] == "same question") == 2


def test_compact_marks_gone_rows(tmp_path):
    """compact() replaces the head with a summary: the old head rows must be
    KEPT (compacted=1, recall still finds them), not deleted, and the summary
    row is inserted once (no re-insert of the surviving tail)."""
    db = _db(tmp_path)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old question one"},
        {"role": "assistant", "content": "old answer one"},
        {"role": "user", "content": "old question two"},
        {"role": "assistant", "content": "old answer two"},
    ]
    db.sync(msgs)
    assert len(_rows(db)) == 4
    # simulate compact(): head replaced by a summary, tail survives
    compacted = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "summary: earlier discussion"},
        {"role": "user", "content": "old question two"},
        {"role": "assistant", "content": "old answer two"},
    ]
    db.sync(compacted)
    rows = _rows(db)
    # 4 old rows (2 marked compacted) + 1 summary = 5
    assert len(rows) == 5, f"expected 5 rows, got {len(rows)}: {rows}"
    by_content = {r[1]: r[4] for r in rows}
    assert by_content["old question one"] == 1  # compacted, kept
    assert by_content["old answer one"] == 1
    assert by_content["old question two"] == 0  # surviving tail, not duplicated
    assert by_content["old answer two"] == 0
    assert by_content["summary: earlier discussion"] == 0
    # the surviving tail was NOT re-inserted (no duplicate rows)
    assert sum(1 for r in rows if r[1] == "old question two") == 1


def test_trim_keeps_tool_row_identity(tmp_path):
    """trim_tool_results() shrinks tool content in place: the row keeps its
    identity (tool_call_id) and is not duplicated on the next sync."""
    db = _db(tmp_path)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c9", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c9", "content": "x" * 5000},
    ]
    db.sync(msgs)
    # trim in place (same dict, shorter content)
    msgs[3]["content"] = "…[trimmed]"
    db.sync(msgs)
    rows = _rows(db)
    assert len(rows) == 3, f"expected 3 rows, got {len(rows)}: {rows}"
    tool_rows = [r for r in rows if r[0] == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0][3] == "c9"


def test_recall_finds_synced_text(tmp_path):
    """recall() searches HISTORY: non-compacted rows of the CURRENT session are
    excluded (they are the live context, already in front of the model). Text
    that left the context (compacted=1) must be found again."""
    db = _db(tmp_path)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "how do I fix the deadlock in the queue worker"},
        {"role": "assistant", "content": "use a condition variable with a generation counter"},
    ]
    db.sync(msgs)
    # live context: not in recall (by design - it is the active context)
    assert db.recall("deadlock") == []
    # simulate compact(): the head leaves the context -> compacted=1
    db.sync([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "summary: earlier discussion"},
        {"role": "assistant", "content": "use a condition variable with a generation counter"},
    ])
    hits = db.recall("deadlock")
    assert hits, "compacted text not found by recall"
    assert any("deadlock" in h for h in hits)
    hits2 = db.recall("generation counter")
    assert any("generation" in h for h in hits2)
    assert db.recall("zzz-no-such-term-zzz") == []


# ---- lifecycle, migration, retention (Session 66 review fixes)
def test_legacy_schema_is_archived_not_dropped(tmp_path):
    """Pre-session DB (no 'session' column): the rows used to be DROPped - real
    history gone. They must survive as a readable turns_legacy_* table, and the
    FTS triggers must not stay bound to the archived table (they would shadow the
    new ones and the index would stay empty)."""
    import sqlite3
    path = str(tmp_path / "legacy.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, role TEXT NOT NULL, content TEXT)")
    con.execute("INSERT INTO turns (ts, role, content) VALUES ('2026-11-01T10:00:00','user','alter verlauf')")
    con.commit()
    con.close()

    db = SessionDB(path)
    legacy = [r[0] for r in db.db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'turns_legacy_%'").fetchall()]
    assert len(legacy) == 1, f"legacy table missing: {legacy}"
    assert db.db.execute(f"SELECT content FROM {legacy[0]}").fetchone()[0] == "alter verlauf"
    # the new schema is complete and the FTS index works end to end
    assert db._schema_count() == len(_SCHEMA)
    db.sync([{"role": "system", "content": "sys"}, {"role": "user", "content": "neuer eintrag"}])
    # no trigger may still point at the archived table
    bound = db.db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                          (legacy[0],)).fetchall()
    assert bound == [], f"triggers still bound to the legacy table: {bound}"
    hits = db.db.execute("SELECT count(*) FROM turns_fts WHERE turns_fts MATCH 'eintrag'").fetchone()[0]
    assert hits == 1, "FTS index not populated after migration"
    db.close()


def test_second_open_is_a_no_op_and_reuses_the_archive_name(tmp_path):
    """Reopening a migrated DB must not create another archive (the schema is
    complete now) - the fast path has to recognise it."""
    import sqlite3
    path = str(tmp_path / "legacy2.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, role TEXT NOT NULL, content TEXT)")
    con.commit()
    con.close()
    with SessionDB(path) as db:
        first = db.session_id
    assert first
    with SessionDB(path) as db:
        n = db.db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name LIKE 'turns_legacy_%'").fetchone()[0]
        assert n == 1, f"re-migration created a duplicate archive ({n} tables)"


def test_close_is_idempotent_and_context_manager_closes(tmp_path):
    """close() must be repeatable (signal/finally paths) and the context manager
    must release the handle: a closed connection raises ProgrammingError."""
    import sqlite3
    import pytest
    db = SessionDB(str(tmp_path / "close.db"))
    db.close()
    db.close()  # second close: no exception
    with pytest.raises(sqlite3.ProgrammingError):
        db.db.execute("SELECT 1")

    with SessionDB(str(tmp_path / "cm.db")) as cm:
        assert cm.session_id
    with pytest.raises(sqlite3.ProgrammingError):
        cm.db.execute("SELECT 1")


def test_prune_keeps_recent_and_drops_old_rows(tmp_path):
    """prune() is the long-runner retention pass: rows older than PRUNE_DAYS go,
    newer ones stay. Called explicitly (the open-time pass runs only once)."""
    import time as _time
    db = SessionDB(str(tmp_path / "prune.db"))
    old_ts = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(_time.time() - (PRUNE_DAYS + 1) * 86400))
    old_sid = db.db.execute("INSERT INTO sessions (ts) VALUES (?)", (old_ts,)).lastrowid
    db.db.execute("INSERT INTO turns (session, ts, role, content) VALUES (?,?,?,?)",
                  (old_sid, old_ts, "user", "uralter eintrag"))
    db.db.execute("INSERT INTO traces (ts, data) VALUES (?,?)", (old_ts, "{}"))
    db.sync([{"role": "system", "content": "sys"}, {"role": "user", "content": "frischer eintrag"}])

    db.prune()
    assert db.db.execute("SELECT count(*) FROM turns WHERE session = ?", (old_sid,)).fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM sessions WHERE id = ?", (old_sid,)).fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM traces WHERE ts = ?", (old_ts,)).fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM turns WHERE session = ?", (db.session_id,)).fetchone()[0] == 1
    # the FTS index followed the delete (trigger), the surviving row is still found
    assert db.db.execute("SELECT count(*) FROM turns_fts WHERE turns_fts MATCH 'frischer'").fetchone()[0] == 1
    db.close()


def test_schema_spec_is_the_single_source_for_the_fast_path(tmp_path):
    """The old fast path counted against a hard-coded 7. Now the count comes from
    _SCHEMA: a complete DB matches exactly, a fresh DB builds it."""
    db = SessionDB(str(tmp_path / "spec.db"))
    assert db._schema_count() == len(_SCHEMA) == 7
    db.db.execute("DROP TRIGGER turns_ad")
    assert db._schema_count() == len(_SCHEMA) - 1  # a missing object is detected
    db.close()


def test_closed_flag_marks_shutdown(tmp_path):
    """db_sync() in the App skips writes once closed - a straggler daemon thread
    must not raise ProgrammingError on exit."""
    db = SessionDB(str(tmp_path / "flag.db"))
    assert db.closed is False
    db.close()
    assert db.closed is True
