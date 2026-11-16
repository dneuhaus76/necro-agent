"""Telegram busy indicator + progress milestone every 20 steps.
- a telegram turn announces 'busy' at start and 'done' before the final reply
  (the user sees when the bot is working / finished); TUI turns stay silent
- the progress milestone fires every TG_MILESTONE_EVERY (20) steps, not 5
"""
import threading
import time


class _Agent:
    """Scripted agent: the first N stream() calls return one tool call each,
    the call after that returns the final text (no calls)."""

    def __init__(self, n_calls, final="final reply"):
        self.n_calls, self.final = n_calls, final
        self._i = 0
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def reset_counters(self):
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def compact(self):
        pass

    def stream(self, tools=None):
        if self._i < self.n_calls:
            self._i += 1
            # unique args per call: identical (name, args, result) 3x in a row
            # trips the stagnation check in App.turn
            text, calls = "", [{"id": f"c{self._i}", "name": "bash", "args": '{"n": ' + str(self._i) + "}"}]
        else:
            text, calls = self.final, []
        return text, calls

    def run_calls(self, calls):
        return [(c, "ok", 1) for c in calls]


def _app(n_calls, final="final reply"):
    import necro
    app = necro.App.__new__(necro.App)
    sent = []
    app.tg = type("TG", (), {"send": staticmethod(lambda cid, t: sent.append((cid, t))),
                             "typing": staticmethod(lambda cid: None)})()
    app.sent = sent
    app.agent = _Agent(n_calls, final)
    app.sessions = type("S", (), {"trace": staticmethod(lambda **k: None)})()
    app.stop, app.lock = threading.Event(), threading.RLock()
    app.blocks, app.scroll = [], 0
    app.step, app.dirty, app.turns = 0, False, 0
    app.messages, app.ctx_used, app.last_completion_tokens = [], 0, 0
    app.busy, app.tab_hint, app.menu, app._queued, app._tg_queued = False, "", None, False, None
    app.db_sync = lambda: None
    return app


def test_tg_turn_announces_busy_and_done(monkeypatch):
    """a telegram turn sends 'busy' at start and 'done' right before the final reply."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(1)
    app._start_turn("d", "c", 7)  # real start: busy message + turn thread
    deadline = time.time() + 10
    while app.busy and time.time() < deadline:
        time.sleep(0.05)
    sent = [t for cid, t in app.sent]
    assert sent[0] == "🕸 busy"  # first message of the turn
    assert "✅ done" in sent
    assert sent.index("✅ done") < sent.index("final reply")  # done before the reply
    assert sent[-1] == "final reply"


def test_tui_turn_stays_silent(monkeypatch):
    """a TUI turn (chat_id=None) sends nothing to telegram."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(1)
    app._start_turn("d", "c")  # no chat_id
    deadline = time.time() + 10
    while app.busy and time.time() < deadline:
        time.sleep(0.05)
    assert app.sent == []


def test_milestone_every_20_steps(monkeypatch):
    """the progress milestone fires at step 20 (TG_MILESTONE_EVERY), not at step 5."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    assert necro.TG_MILESTONE_EVERY == 20
    app = _app(21)  # 21 tool steps, then the final reply
    necro.App.turn(app, chat_id=7)
    sent = [t for cid, t in app.sent]
    milestones = [t for t in sent if t.startswith("⏳ milestone")]
    assert len(milestones) == 1  # only step 20 (step 5 no longer triggers)
    assert "step 20/" in milestones[0]
    assert "step 5/" not in sent


def test_milestone_not_below_20_steps(monkeypatch):
    """a short telegram turn (fewer than 20 steps) sends no milestone at all."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(6)
    necro.App.turn(app, chat_id=7)
    sent = [t for cid, t in app.sent]
    assert not any(t.startswith("⏳ milestone") for t in sent)


class _GatedAgent:
    """Agent that blocks on a per-call release event (keyed by (turn, step)) -
    used to queue a prompt while the turn is still running (N216). The test
    sets release[(turn, step)] to an Event and releases it when it wants that
    particular stream() call to proceed, so both turns can be held independently."""

    def __init__(self):
        self.release = {}  # (turn, step) -> release Event, set by the test
        self._turn = 0
        self._i = 0
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    def reset_counters(self):
        self._deleg_fallbacks, self._deleg_steps = 0, 0
        self._turn += 1  # per-turn: each turn gets its own call sequence
        self._i = 0

    def compact(self):
        pass

    def stream(self, tools=None):
        if self._i == 0:
            self._i += 1
            return "", [{"id": "c1", "name": "bash", "args": '{"n": 1}'}]
        # steps >= 1 block until the test releases (turn, step)
        key = (self._turn, self._i)
        ev = self.release.setdefault(key, threading.Event())
        ev.wait(3)
        self._i += 1
        if self._i >= 3:  # third call of a turn = final reply
            return "final", []
        return "", [{"id": f"c{self._i}", "name": "bash", "args": '{"n": ' + str(self._i) + "}"}]

    def run_calls(self, calls):
        return [(c, "ok", 1) for c in calls]


def test_queued_prompt_runs_at_step_boundary(monkeypatch):
    """a prompt queued while the turn is running starts its own turn at the
    next step boundary - the old turn exits right there (its finally must not
    clobber the new turn's busy/step state), the new turn runs to completion."""
    import necro
    monkeypatch.setattr(necro, "REMOTE", False)
    app = _app(1)
    agent = _GatedAgent()
    app.agent = agent
    app.add = lambda kind, text: None
    app.history, app._hist_i, app.buf, app.cur, app.tab_hint, app.menu = [], 0, "", 0, "", None
    app._queued_block = None
    app.busy = True  # _start_turn would set this; we call turn() directly
    t_old = threading.Thread(target=necro.App.turn, args=(app,), daemon=True)
    t_old.start()
    deadline = time.time() + 10
    while (1, 1) not in agent.release and time.time() < deadline:
        time.sleep(0.02)
    assert (1, 1) in agent.release  # old turn is mid-run (blocked in its 2nd stream call)
    app.submit("queued prompt")  # queued while busy
    assert app._queued
    agent.release[(1, 1)].set()  # old turn returns c2, loops to step 3 -> queued-check fires
    deadline = time.time() + 10
    while app.turns < 2 and time.time() < deadline:  # the queued prompt starts its own turn
        time.sleep(0.02)
    assert app.turns == 2
    deadline = time.time() + 10
    while t_old.is_alive() and time.time() < deadline:  # wait until the old turn is gone
        time.sleep(0.02)
    assert not t_old.is_alive()  # the old turn exited at the step boundary (not at turn end)
    assert app.busy is True  # the old turn's finally must not have reset the new turn's state
    assert app.step >= 1  # ... and must not have zeroed the new turn's step counter
    deadline = time.time() + 10
    while (2, 1) not in agent.release and time.time() < deadline:
        time.sleep(0.02)
    agent.release[(2, 1)].set()  # let the new turn finish
    deadline = time.time() + 10
    while (2, 2) not in agent.release and time.time() < deadline:
        time.sleep(0.02)
    agent.release[(2, 2)].set()
    deadline = time.time() + 10
    while sum(1 for m in app.messages if m.get("content") == "final") < 1 and time.time() < deadline:
        time.sleep(0.05)
    assert any(m.get("content") == "queued prompt" for m in app.messages)  # the new turn ran it
    assert sum(1 for m in app.messages if m.get("content") == "final") >= 1  # the new turn ended
    assert app.busy is False and app.step == 0  # the new turn's own finally cleaned up
