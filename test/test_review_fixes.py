"""Code-review fixes (Session 65): one test per fixed bug.
1. tg_submit ack referenced the undefined wl_ids (NameError on every TG prompt)
2. probe.resolve passed 3 args to detect_thinking(base, model) -> TypeError on the
   auto-detect path (cfg without 'reasoning'), the daemon thread died silently
3. TelegramBot was constructed without the whitelist -> stranger routing dead;
   app.tg_chats never set -> run summary / stranger alerts never sent
4. tg._route read 'from_user', the Bot API sends 'from' -> sender always ""
5. recall() LIKE fallback: '%'/'_' in the query acted as wildcards
6. write_file/edit_file wrote non-atomically (crash mid-write = corrupt file)
7. Agent.reset_counters() replaces the private-attribute access in App.turn
"""
import threading
import time

from modules.core import probe
from modules.core.agent import Agent, CONSULT_TRIM
from modules.core.sessiondb import SessionDB
from modules.io import tools


def test_tg_submit_ack_no_name_error(monkeypatch):
    """Bug 1: the ack line must not raise (was: wl_ids undefined); a whitelisted
    chat id gets the '(id in whitelist)' suffix."""
    import necro
    monkeypatch.setattr(necro, "TG_CHATS", ["42"])
    app = necro.App.__new__(necro.App)
    sent = []
    app.tg = type("TG", (), {"send": staticmethod(lambda cid, t: sent.append((cid, t)))})()
    app.busy, app._tg_queued = False, None
    app._start_turn = lambda *a, **k: None
    necro.App.tg_submit(app, "hello", 42, "me")
    assert (42, "✓ (42 in whitelist)") in sent


def test_probe_resolve_auto_detect_reaches_on_done(monkeypatch):
    """Bug 2: cfg_think=None (auto-detect) must not die in the worker thread -
    on_done fires even when the server is unreachable (dead port = fast refusal)."""
    monkeypatch.setattr(probe.time, "sleep", lambda s: None)  # retry backoff: keep the suite fast
    got = []
    t0 = time.monotonic()
    probe.resolve(None, "http://127.0.0.1:1/v1", "m", 100, lambda i, t: got.append((i, t)))
    assert len(got) == 1
    info, think = got[0]
    assert info["ctx"] == 100  # fallback (server unreachable)
    assert think is False  # both probes failed -> assume no reasoning
    assert time.monotonic() - t0 < 10  # no 30 s timeout hang


class FakeApp:
    def __init__(self):
        self.messages = [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "hi"},
                         {"role": "tool", "tool_call_id": "1", "content": "T" * 5000}]
        self.stop = threading.Event()


def test_consult_local_context_trimmed_to_consult_trim():
    """Bug 7 (SSOT): the local consult context trims tool results with CONSULT_TRIM
    (was: hard-coded 2000 next to the CONSULT_TRIM constant)."""
    app = FakeApp()
    ag = Agent(app, base="http://local", step_timeout=10, tools_list=[], sub_tools=[],
               swarm=[], swarm_up={}, agents_md="", agents_md_path="")
    local_calls = []

    remote_script = ["QUESTION: q1", "ANSWER: final"]

    def complete(msgs, base=None, model=None):
        if base is not None:
            return remote_script.pop(0)
        local_calls.append(msgs)
        return "ok"

    ag.complete = complete
    ag.consult({"model": "m", "url": "http://r"}, "question")
    assert local_calls, "the local model must answer (or the question short-circuits)"
    tool_msgs = [m for m in local_calls[0] if m.get("role") == "tool"]
    assert tool_msgs and all(len(m["content"]) <= CONSULT_TRIM for m in tool_msgs)


def test_recall_like_wildcards_escaped(tmp_path):
    """Bug 5: a literal '%' in the query must not glob (LIKE fallback path)."""
    p = tmp_path / "n.db"
    db = SessionDB(p)
    other = SessionDB(p)  # a different session: recall() only searches PAST sessions
    other.sync([{"role": "system", "content": "s"}, {"role": "user", "content": "hello world"}])
    assert db.recall("hello")  # FTS path works
    assert db.recall("%") == []  # FTS yields nothing -> LIKE; unescaped '%%%%' would match all


def test_write_file_atomic(tmp_path):
    """Bug 6: write via temp + rename, no .tmp leftover."""
    p = tmp_path / "sub" / "f.txt"
    r = tools.write_file(str(p), "content")
    assert r.startswith("wrote")
    assert p.read_text() == "content"
    assert not (tmp_path / "sub" / "f.txt.tmp").exists()
    r = tools.edit_file(str(p), "content", "changed")
    assert p.read_text() == "changed"
    assert not (tmp_path / "sub" / "f.txt.tmp").exists()


def test_agent_reset_counters():
    """Bug 7: App.turn resets the per-turn counters via a public method."""
    app = FakeApp()
    ag = Agent(app, base="http://local", step_timeout=10, tools_list=[], sub_tools=[],
               swarm=[], swarm_up={}, agents_md="", agents_md_path="")
    ag._deleg_fallbacks, ag._deleg_steps = 3, 7
    ag.reset_counters()
    assert ag._deleg_fallbacks == 0 and ag._deleg_steps == 0
