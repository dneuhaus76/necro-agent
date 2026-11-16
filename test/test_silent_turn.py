"""a turn that produced only reasoning (no content, no tool calls) ends
silently in the TUI - the user sees the thinking block, then an idle prompt,
and assumes it stalled. Fix: show an info marker so "done with no visible
output" is visible. Telegram turns are unaffected (they already send
'(no output)')."""
import threading
import time


class _SilentAgent:
    """Agent whose stream() returns empty content and no tool calls on the
    first call - the turn ends immediately with no visible output."""

    def __init__(self):
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def reset_counters(self):
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def compact(self):
        pass

    def stream(self, tools=None):
        return "", []  # no content, no calls -> turn ends

    def run_calls(self, calls):
        return [(c, "ok", 1) for c in calls]


class _ContentAgent:
    """Agent whose stream() returns real content - no info marker needed."""

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


def _app(agent):
    import necro
    app = necro.App.__new__(necro.App)
    sent = []
    app.tg = type("TG", (), {"send": staticmethod(lambda cid, t: sent.append((cid, t))),
                             "typing": staticmethod(lambda cid: None)})()
    app.sent = sent
    app.agent = agent
    app.sessions = type("S", (), {"trace": staticmethod(lambda **k: None)})()
    app.stop, app.lock = threading.Event(), threading.RLock()
    app.blocks, app.scroll = [], 0
    app.step, app.dirty, app.turns = 0, False, 0
    app.messages, app.ctx_used, app.last_completion_tokens = [], 0, 0
    app.busy, app.tab_hint, app.menu, app._queued, app._tg_queued = False, "", None, False, None
    app.db_sync = lambda: None
    return app


def test_tui_silent_turn_shows_info_marker(monkeypatch):
    """a TUI turn with no content and no tool calls shows an info block."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(_SilentAgent())
    necro.App.turn(app)  # chat_id=None (TUI)
    info_blocks = [b.text for b in app.blocks if b.kind == "info"]
    assert any("no output" in t for t in info_blocks)


def test_tui_content_turn_no_marker(monkeypatch):
    """a TUI turn with real content does NOT show the info marker."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(_ContentAgent())
    necro.App.turn(app)
    info_blocks = [b.text for b in app.blocks if b.kind == "info"]
    assert not any("no output" in t for t in info_blocks)


def test_tg_silent_turn_unchanged(monkeypatch):
    """a telegram turn with no content still sends '(no output)' (not the info block)."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(_SilentAgent())
    necro.App.turn(app, chat_id=7)
    sent = [t for cid, t in app.sent]
    assert "(no output)" in sent
    # no info block in the TUI for a TG turn
    info_blocks = [b.text for b in app.blocks if b.kind == "info"]
    assert not any("no output" in t for t in info_blocks)
