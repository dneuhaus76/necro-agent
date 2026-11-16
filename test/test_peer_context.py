"""a peer turn must carry an explicit sibling-context line, not just the
[frm] prefix the model has to infer the relationship from. The worker builds the
user message as:

    {peer_tag} [{frm}]
    {task}

The [frm] prefix alone carries the sibling context (N234 added a redundant
'Act as sibling of {frm}' line that duplicated the sender-side task prefix and
was removed in N237). peer_tag stays line 1 for UI detection. frm is the DIRECT
caller (the sibling this turn is for) - no original-sender tracking. The task
text itself must stay intact (it is the payload)."""
import json
import threading
import time
import urllib.request


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
        self._last_reply = None
        self._last_turn_error = None
        self.reply = None

    def add(self, kind, text="", color=None):
        self.messages.append({"role": kind, "content": text})
        return None

    def db_sync(self):
        pass

    def turn(self, chat_id=None, done=None):
        self.busy = True
        time.sleep(0.02)
        self._last_turn_error = None
        if self.reply is not None:
            m = {"role": "assistant", "content": self.reply}
            self.messages.append(m)
            self._last_reply = m
        self.busy = False
        if done is not None:
            done.set()


def _fixture(tmp_path, monkeypatch):
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


def _last_user(app):
    return [m for m in app.messages if m["role"] == "user"][-1]["content"]


def test_peer_turn_carries_sibling_context(tmp_path, monkeypatch):
    """the user message the model sees must contain the explicit sibling line."""
    api_mod, p, app = _fixture(tmp_path, monkeypatch)
    app.reply = "ok"
    status, body = _post(p, "review the peer code", frm="abcd1234")
    assert status == 200
    assert body["result"] == "ok"
    user = _last_user(app)
    assert "Act as sibling of abcd1234." not in user  # redundant line removed
    assert "[abcd1234]" in user


def test_peer_turn_task_text_intact(tmp_path, monkeypatch):
    """the task payload must survive the context line unchanged (as its own line)."""
    api_mod, p, app = _fixture(tmp_path, monkeypatch)
    app.reply = "ok"
    task = "review the peer code"
    _post(p, task, frm="abcd1234")
    user = _last_user(app)
    # the task is the second line, verbatim - the context line is prepended, not merged
    assert user.split("\n", 1)[1] == task
    # the context line comes first and starts with the peer tag (detection + rendering)
    assert user.startswith(app.peer_tag)


def test_peer_turn_unknown_sender(tmp_path, monkeypatch):
    """a POST without 'from' falls back to '?' - the context line still forms."""
    api_mod, p, app = _fixture(tmp_path, monkeypatch)
    app.reply = "ok"
    req = urllib.request.Request(
        f"http://127.0.0.1:{p.port}/api/prompt",
        json.dumps({"task": "task"}).encode(),
        {"Content-Type": "application/json", "X-Necro-Token": p.token})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200
    user = _last_user(app)
    assert "Act as sibling of ?." not in user  # redundant line removed
