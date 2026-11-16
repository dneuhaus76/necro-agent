"""a peer turn that ends without a reply must tell the PARENT why, not just
'(no output)'. Two causes used to be indistinguishable:

  1. a clean reasoning-only end (N232) - no content, no calls, no error
  2. an abort (watchdog timeout, stagnation, exception) - no new assistant msg

do_POST now returns '(aborted: <reason>)' when the turn set _last_turn_error and
'(no output)' for a clean reasoning-only end. The App.turn except-block stores the
reason and tells a watchdog timeout apart from a user abort (both set stop)."""
import json
import threading
import time
import urllib.request


# ---------------------------------------------------------------------------
# do_POST level: the parent-facing result string
# ---------------------------------------------------------------------------

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
        # what the fake turn does: set _last_turn_error and/or append a reply
        self.turn_error = None
        self.reply = None

    def add(self, kind, text="", color=None):
        self.messages.append({"role": kind, "content": text})
        return None

    def db_sync(self):
        pass

    def turn(self, chat_id=None, done=None):
        self.busy = True
        time.sleep(0.02)
        # mirror the real App.turn: reset at start, set on abort, None on clean end
        self._last_turn_error = self.turn_error
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


def _post(p, task, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{p.port}/api/prompt",
        json.dumps({"task": task, "from": "other"}).encode(),
        {"Content-Type": "application/json", "X-Necro-Token": p.token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def test_do_post_reports_abort_reason(tmp_path, monkeypatch):
    """a turn that aborted (stagnation) and produced no reply -> parent gets the reason."""
    api_mod, p, app = _fixture(tmp_path, monkeypatch)
    app.turn_error = "Stagnation: 'bash' 3x in a row with the same result - turn aborted"
    app.reply = None
    status, body = _post(p, "task1")
    assert status == 200
    assert body["result"].startswith("(aborted: ")
    assert "Stagnation" in body["result"]


def test_do_post_clean_no_output(tmp_path, monkeypatch):
    """a clean reasoning-only end (no error) -> '(no output)' (N232 behaviour kept)."""
    api_mod, p, app = _fixture(tmp_path, monkeypatch)
    app.turn_error = None
    app.reply = None
    status, body = _post(p, "task1")
    assert status == 200
    assert body["result"] == "(no output)"


def test_do_post_reply_wins_over_error(tmp_path, monkeypatch):
    """a turn that both replied and (later) set an error still returns the reply."""
    api_mod, p, app = _fixture(tmp_path, monkeypatch)
    app.turn_error = "RuntimeError: boom"
    app.reply = "the actual answer"
    status, body = _post(p, "task1")
    assert status == 200
    assert body["result"] == "the actual answer"


# ---------------------------------------------------------------------------
# App.turn except-block level: watchdog timeout vs. user abort vs. exception
# ---------------------------------------------------------------------------

def _app(agent):
    import necro
    app = necro.App.__new__(necro.App)
    app.tg = type("TG", (), {"send": staticmethod(lambda cid, t: None),
                             "typing": staticmethod(lambda cid: None)})()
    app.agent = agent
    app.sessions = type("S", (), {"trace": staticmethod(lambda **k: None)})()
    app.stop, app.lock = threading.Event(), threading.RLock()
    app.blocks, app.scroll = [], 0
    app.step, app.dirty, app.turns = 0, False, 0
    app.messages, app.ctx_used, app.last_completion_tokens = [], 0, 0
    app.busy, app.tab_hint, app.menu, app._queued, app._tg_queued = False, "", None, False, None
    app.conns = []
    app.db_sync = lambda: None
    return app


class _HangingAgent:
    """stream() waits for stop (like a hung LLM call), then raises 'aborted' -
    mirroring the real turn loop's stop-check -> RuntimeError('aborted')."""
    def __init__(self, app):
        self.app = app
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def reset_counters(self):
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def compact(self):
        pass

    def stream(self, tools=None):
        self.app.stop.wait(5)  # hang until the watchdog fires
        raise RuntimeError("aborted")


class _RaisingAgent:
    """stream() raises immediately - a plain exception (no stop)."""
    def __init__(self):
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def reset_counters(self):
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def compact(self):
        pass

    def stream(self, tools=None):
        raise RuntimeError("boom")


def test_turn_except_block_watchdog_timeout(monkeypatch):
    """a watchdog timeout (stop set + _watchdog_fired) -> 'turn timeout (watchdog, N s)',
    not a bare 'aborted'."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(None)  # agent set below (needs app for stop.wait)
    app.agent = _HangingAgent(app)
    # start the watchdog exactly like PeerAPI._peer_worker does
    app._watchdog_fired = None
    def _wd_fire():
        app._watchdog_fired = 0.15
        app.stop.set()
    threading.Timer(0.15, _wd_fire).start()
    necro.App.turn(app)  # chat_id=None (TUI)
    assert app._last_turn_error is not None
    assert "turn timeout (watchdog" in app._last_turn_error
    assert "0.15 s" in app._last_turn_error


def test_turn_except_block_plain_exception(monkeypatch):
    """a plain exception (no stop) -> the exception type + message is stored."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(_RaisingAgent())
    app._watchdog_fired = None
    necro.App.turn(app)
    assert app._last_turn_error == "RuntimeError: boom"


def test_turn_clean_end_resets_error(monkeypatch):
    """a clean turn (content, no calls) leaves _last_turn_error = None."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)

    class _ContentAgent:
        def __init__(self):
            self._deleg_fallbacks, self._deleg_steps = 0, 0
        def reset_counters(self):
            self._deleg_fallbacks, self._deleg_steps = 0, 0
        def compact(self):
            pass
        def stream(self, tools=None):
            return "hello", []
        def run_calls(self, calls):
            return [(c, "ok", 1) for c in calls]

    app = _app(_ContentAgent())
    app._last_turn_error = "stale from a previous turn"  # must be reset at turn start
    necro.App.turn(app)
    assert app._last_turn_error is None
