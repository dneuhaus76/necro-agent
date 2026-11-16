"""incoming peer turns are queued (no more 409) - a second POST while the
peer is busy waits for the running turn and gets its own result (200)."""
import json
import os
import threading
import time
import urllib.request

import pytest


class FakeApp:
    def __init__(self):
        self.lock = threading.RLock()
        self.busy = False
        self.messages = []
        self.scroll = 0
        self.dirty = True
        self.peer_tag = "📓"
        self.stop = threading.Event()
        self._peer_turn = False
        self.turns = []  # (task, done) - executed turns in order
        self._running = threading.Event()  # set while a fake turn runs
        self._last_reply = None  # mirrors App: identity of the last assistant msg appended

    def add(self, kind, text="", color=None):
        self.messages.append({"role": kind, "content": text})
        return None

    def db_sync(self):
        pass

    def turn(self, chat_id=None, done=None):
        # the worker calls add() before turn() - use the last message added by add()
        task = self.messages[-1]["content"] if self.messages else ""
        # strip the peer tag prefix ("📓 [other]\n<task>") to get the raw task
        task = task.split("]\n", 1)[-1] if "]\n" in task else task
        self.busy = True
        self._running.set()
        time.sleep(0.05)  # short window: the second POST arrives while this turn is busy
        self._running.clear()
        m = {"role": "assistant", "content": f"reply-{task}"}
        self.messages.append(m)
        self._last_reply = m  # mirrors the real App turn loop (necro.py)
        self.busy = False
        self.turns.append(task)
        if done is not None:
            done.set()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_PEER_DIR", str(tmp_path / "peers"))
    from modules.io import api as api_mod
    app = FakeApp()
    p = api_mod.PeerAPI(app, host="127.0.0.1", port=0)
    return api_mod, p, app


def _post(p, task, frm="other", timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{p.port}/api/prompt",
        json.dumps({"task": task, "from": frm}).encode(),
        {"Content-Type": "application/json", "X-Necro-Token": p.token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())



def test_second_post_is_queued_not_409(api):
    api_mod, p, app = api
    results = []
    t2 = threading.Thread(target=lambda: results.append(_post(p, "task2")))
    t1 = threading.Thread(target=lambda: results.append(_post(p, "task1")))
    t1.start()
    assert app._running.wait(5)  # first turn is running (busy) when the second arrives
    t2.start()
    t1.join(15)
    t2.join(15)
    assert [r[0] for r in results] == [200, 200]  # no 409 anywhere
    assert results[0][1]["result"] == "reply-task1"
    assert results[1][1]["result"] == "reply-task2"
    assert app.turns == ["task1", "task2"]  # FIFO order



def test_two_queued_items_no_deadlock(api):
    """two items in the queue simultaneously must not deadlock the worker.
    Old code waited on qsize() after dequeuing item 1, seeing item 2, but only
    the worker can dequeue - so it waited forever (both HTTP senders block)."""
    api_mod, p, app = api
    # make the app busy so the worker waits before processing anything
    app.busy = True
    local_done = threading.Event()

    def fake_local():
        time.sleep(0.15)  # long enough for both POSTs to enqueue (local HTTP ~20 ms)
        app.busy = False
        local_done.set()

    threading.Thread(target=fake_local, daemon=True).start()
    results = {}
    t1 = threading.Thread(target=lambda: results.update(t1=_post(p, "task1")))
    t2 = threading.Thread(target=lambda: results.update(t2=_post(p, "task2")))
    t1.start()
    t2.start()
    t1.join(15)
    t2.join(15)
    assert results["t1"][0] == 200
    assert results["t2"][0] == 200
    assert results["t1"][1]["result"] == "reply-task1"
    assert results["t2"][1]["result"] == "reply-task2"
    assert sorted(app.turns) == ["task1", "task2"]  # both processed (order depends on arrival)



def test_worker_waits_for_local_turn(api):
    """A local turn that starts after the previous peer turn ended must finish
    before the worker starts the queued peer turn (no two turns at once)."""
    api_mod, p, app = api
    r1 = _post(p, "task1")
    assert r1[0] == 200
    # simulate a local turn running while a new peer POST arrives
    app.busy = True
    local_done = threading.Event()

    def fake_local():
        time.sleep(0.15)  # long enough for the second POST to enqueue (local HTTP ~20 ms)
        app.busy = False
        local_done.set()

    threading.Thread(target=fake_local, daemon=True).start()
    r2 = _post(p, "task2", timeout=15)
    assert r2[0] == 200
    assert r2[1]["result"] == "reply-task2"
    assert local_done.is_set()  # the local turn finished before the peer turn ran
    assert app.turns == ["task1", "task2"]



def test_last_reply_set_after_peer_turn(api):
    """after a peer turn, app._last_reply is the assistant message dict
    that was appended (identity, not a copy)."""
    api_mod, p, app = api
    assert app._last_reply is None  # fresh app
    r = _post(p, "task1")
    assert r[0] == 200
    assert app._last_reply is not None
    assert app._last_reply["role"] == "assistant"
    assert app._last_reply["content"] == "reply-task1"
    # identity: the dict in messages is the same object (not a copy)
    assert app._last_reply is app.messages[-1]



def test_do_post_returns_last_reply(api):
    """do_POST returns the reply via app._last_reply (decoupled from the
    tail-invariant), and a stale _last_reply from an earlier turn is rejected
    by the before-set (id not in before)."""
    api_mod, p, app = api
    r1 = _post(p, "task1")
    assert r1[0] == 200
    assert r1[1]["result"] == "reply-task1"
    # second turn: _last_reply from turn 1 is stale (in before) -> not returned
    r2 = _post(p, "task2")
    assert r2[0] == 200
    assert r2[1]["result"] == "reply-task2"


def test_do_post_no_output_when_last_reply_none(api):
    """a turn that produces no assistant message leaves _last_reply stale
    (or None on the first turn) -> do_POST returns '(no output)'."""
    api_mod, p, app = api
    # a turn that appends no assistant message at all (error/abort/timeout)
    def silent_turn(chat_id=None, done=None):
        app.busy = True
        app.busy = False
        app.turns.append("silent")
        if done is not None:
            done.set()

    app.turn = silent_turn
    r = _post(p, "silent")
    assert r[0] == 200
    assert r[1]["result"] == "(no output)"
    assert app._last_reply is None  # never set (no assistant msg produced)
