"""Ctrl-C: idle -> clear session (like /clear), busy -> abort the turn."""
import threading

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


def _app(necro):
    app = necro.App()
    app.add("user", "frage")
    app.add("agent", "antwort")
    return app


def test_ctrl_c_idle_clears_session(env):
    app = _app(env)
    app.key("\x03")
    assert not app.quit  # no longer quits when idle
    assert not any(b.text == "frage" for b in app.blocks)  # old blocks cleared
    assert any(b.kind == "info" for b in app.blocks)  # startup info re-added
    assert len(app.messages) == 1  # context reset to the system message
    assert app.scroll == app.ctx_used == app.turns == 0


def test_ctrl_c_busy_aborts(env):
    app = _app(env)
    app.busy = True
    app.key("\x03")
    assert app.stop.is_set()  # turn aborted, no clear
    assert not app.quit


def test_ctrl_c_clears_input_line(env):
    app = _app(env)
    app.buf, app.cur = "hallo welt", 6
    app.key("\x03")
    assert app.buf == "" and app.cur == 0  # idle: input line cleared with the session
    app.buf, app.cur = "hallo welt", 6
    app.busy = True
    app.key("\x03")
    assert app.buf == "" and app.cur == 0  # busy: input line cleared with the abort
