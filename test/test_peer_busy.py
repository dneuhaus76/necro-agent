"""a peer turn marks the instance busy (health endpoint) and a hung peer turn
is aborted by the watchdog after PEER_TURN_TIMEOUT - busy is released on abort
instead of being held until the sender gives up."""
import json
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
        self._running = threading.Event()
        self.stop_seen_during_turn = False

    def add(self, kind, text="", color=None):
        return None

    def db_sync(self):
        pass

    def turn(self, chat_id=None, done=None):
        task = next(m["content"] for m in reversed(self.messages) if m["role"] == "user")
        self.busy = True
        self._running.set()
        time.sleep(0.15)  # short busy window (health check must land inside it)
        self.stop_seen_during_turn = self.stop.is_set()
        self._running.clear()
        self.messages.append({"role": "assistant", "content": f"reply-{task}"})
        self.busy = False
        if done is not None:
            done.set()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_PEER_DIR", str(tmp_path / "peers"))
    from modules.io import api as api_mod
    app = FakeApp()
    p = api_mod.PeerAPI(app, host="127.0.0.1", port=0)
    return api_mod, p, app


def _post(p, task, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{p.port}/api/prompt",
        json.dumps({"task": task, "from": "other"}).encode(),
        {"Content-Type": "application/json", "X-Necro-Token": p.token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def _health(p):
    req = urllib.request.Request(f"http://127.0.0.1:{p.port}/api/health",
                                 headers={"X-Necro-Token": p.token})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_peer_turn_reports_busy(api):
    api_mod, p, app = api
    t = threading.Thread(target=lambda: _post(p, "task1"))
    t.start()
    assert app._running.wait(5)  # peer turn is running
    assert _health(p)["busy"] is True  # visible while the peer turn runs
    t.join(15)
    assert _health(p)["busy"] is False  # released after the turn


def test_hung_turn_is_aborted_by_watchdog(api, monkeypatch):
    api_mod, p, app = api
    p.turn_timeout = 0.2  # short watchdog for the test (now per-instance, cfg 'peer_turn_timeout')

    def hung_turn(chat_id=None, done=None):
        app.busy = True
        app._running.set()
        time.sleep(0.3)  # simulates a hang (no stop check inside); > watchdog 0.2s
        app.stop_seen_during_turn = app.stop.is_set()
        app._running.clear()
        app.busy = False
        app.messages.append({"role": "assistant", "content": "late reply"})
        if done is not None:
            done.set()

    app.turn = hung_turn
    status, body = _post(p, "task1", timeout=15)
    assert status == 200
    deadline = time.time() + 10
    while app._running.is_set() and time.time() < deadline:  # wait for the hung turn to end
        time.sleep(0.1)
    assert app.stop_seen_during_turn  # the watchdog aborted the hung turn
    assert app.busy is False  # busy released, not held until the sender gave up


def test_discovery_file_carries_turn_timeout(api):
    api_mod, p, app = api
    d = json.loads(p.path.read_text())
    assert d["peer_turn_timeout"] == p.turn_timeout  # written at startup


def test_peer_prompt_uses_peers_own_timeout(api, monkeypatch):
    """the caller waits for the PEER's configured peer_turn_timeout * 2 + 30 s
    (queue wait behind a running turn + the turn itself), read from its
    discovery file - not the caller's own value."""
    api_mod, p, app = api
    # the peer's configured value must be in its discovery file
    d = json.loads(p.path.read_text())
    d["peer_turn_timeout"] = 1234
    p.path.write_text(json.dumps(d))
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["timeout"] = timeout
        raise ConnectionError("stop here")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)  # patched at the source (peer_prompt imports it locally)
    inst = json.loads(p.path.read_text())
    api_mod.peer_prompt(inst, "task")
    assert captured["timeout"] == 1234 * 2 + 30


def test_peer_prompt_malformed_discovery_falls_back(api, monkeypatch):
    """a broken peer_turn_timeout in the discovery file must not crash the
    caller - it falls back to the local PEER_TURN_TIMEOUT (* 2 + 30 s)."""
    api_mod, p, app = api
    d = json.loads(p.path.read_text())
    d["peer_turn_timeout"] = "not-a-number"
    p.path.write_text(json.dumps(d))
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["timeout"] = timeout
        raise ConnectionError("stop here")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    inst = json.loads(p.path.read_text())
    api_mod.peer_prompt(inst, "task")  # must not raise
    assert captured["timeout"] == api_mod.PEER_TURN_TIMEOUT * 2 + 30


def test_discovery_write_is_atomic(api):
    """the discovery file is written via tmp + os.replace - no .tmp file
    remains and the final file is valid JSON."""
    api_mod, p, app = api
    d = json.loads(p.path.read_text())  # valid JSON after the atomic write
    assert d["id"] == p.id
    assert not list(api_mod.PEER_DIR.glob("*.tmp"))  # no leftover tmp file
