"""spin_only draw while NOT busy must not write the input line: the spinner frame
is only valid while a turn runs. Writing it idle would cache a braille char in
_last_view that the next full draw then skips - a stale spinner frame stays on
screen after the turn ends (the user sees a braille char instead of the prompt)."""
import os

import pytest
from modules.ui import view as view_mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


def _draw(necro, app, monkeypatch, w, h, spin_only=False):
    monkeypatch.setattr(view_mod.shutil, "get_terminal_size", lambda *a: os.terminal_size((w, h)))
    out = []
    monkeypatch.setattr(necro.os, "write", lambda fd, data: out.append(data))
    app.draw(spin_only=spin_only)
    return b"".join(out)


def test_spin_only_idle_does_not_write_input_line(env, monkeypatch):
    """busy spin_only tick writes the spinner frame; the turn ends (busy=False,
    dirty=True); the spin_only tick that races the end must NOT rewrite the input
    line - the full draw that follows owns the prompt marker."""
    necro = env
    app = necro.App()
    app.add("user", "frage")
    app.buf, app.cur = "hi", 2
    monkeypatch.setattr(necro.time, "time", lambda: 1000.0)
    _draw(necro, app, monkeypatch, 80, 24)  # idle baseline
    app.busy = True
    monkeypatch.setattr(necro.time, "time", lambda: 1000.1)  # new spinner frame
    out = _draw(necro, app, monkeypatch, 80, 24, spin_only=True)
    assert b"\x1b[21;1H" in out, "busy spin_only tick must refresh the input line"
    # the turn ends between two ticks: busy flips to False, a spin_only tick fires
    # before the full draw (the main loop saw busy on the previous select slice)
    app.busy = False
    monkeypatch.setattr(necro.time, "time", lambda: 1000.2)
    out = _draw(necro, app, monkeypatch, 80, 24, spin_only=True)
    assert b"\x1b[21;1H" not in out, \
        "idle spin_only tick must not write the input line (stale braille frame)"
    # and the cache must not hold the spinner frame: the full draw must rewrite
    # the prompt marker, not skip the row
    out = _draw(necro, app, monkeypatch, 80, 24)
    assert b"\x1b[21;1H" in out, \
        "full draw after an idle spin_only tick must restore the prompt marker"
