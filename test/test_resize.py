"""resize (SIGWINCH) - rows written at the old layout that fall OUTSIDE the
new one must be cleared, otherwise stale lines stay on screen and the content-area
line spacing looks broken after a window resize + scroll."""
import os
import tempfile

import pytest
from modules.ui import view as view_mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB + import necro (no side effects beyond cfg read)."""
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


def _draw(necro, app, monkeypatch, w, h):
    """One full draw at terminal size (w, h); returns the written bytes."""
    monkeypatch.setattr(view_mod.shutil, "get_terminal_size", lambda *a: os.terminal_size((w, h)))
    out = []
    monkeypatch.setattr(necro.os, "write", lambda fd, data: out.append(data))
    app.dirty = True
    app.draw()
    return b"".join(out)


def _app(necro):
    app = necro.App()
    app.add("user", "hallo welt")
    app.add("agent", "antwort des agents")
    return app


def test_resize_clears_stale_rows(env, monkeypatch):
    necro = env
    app = _app(necro)
    _draw(necro, app, monkeypatch, 80, 24)  # first draw at 80x24 (ch=15, input row 21)
    out = _draw(necro, app, monkeypatch, 100, 30)  # grow: ch=21, input row 27
    # old footer rows (19, 20, 22, 23, 24) are outside the new layout -> cleared
    for r in (19, 20, 22, 23, 24):
        assert f"\x1b[{r};1H\x1b[2K".encode() in out, f"row {r} not cleared after grow"


def test_shrink_clears_rows_below_new_layout(env, monkeypatch):
    necro = env
    app = _app(necro)
    _draw(necro, app, monkeypatch, 80, 24)  # ch=15, footer rows up to 24
    out = _draw(necro, app, monkeypatch, 80, 12)  # shrink: ch=3, footer rows 7..12
    # old content/footer rows 13..24 are outside the new layout -> cleared
    for r in (13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24):
        assert f"\x1b[{r};1H\x1b[2K".encode() in out, f"row {r} not cleared after shrink"


def test_same_size_no_clears(env, monkeypatch):
    necro = env
    app = _app(necro)
    _draw(necro, app, monkeypatch, 80, 24)
    out = _draw(necro, app, monkeypatch, 80, 24)  # same size: no stale rows
    assert out == b"", "no-op redraw must write nothing"


def test_resize_clears_old_input_row(env, monkeypatch):
    necro = env
    app = _app(necro)
    _draw(necro, app, monkeypatch, 80, 24)  # ch=15, input row y+2 = 21
    out = _draw(necro, app, monkeypatch, 80, 12)  # shrink: ch=3, input row 9
    # the input line is written outside the rows loop -> it must be cleared at its old row
    assert b"\x1b[21;1H\x1b[2K" in out, "old input row 21 not cleared after shrink"


def test_resize_cursor_back_to_input_col(env, monkeypatch):
    necro = env
    app = _app(necro)
    app.ins("hallo welt")
    app.cur = 3  # cursor mid-text: col 3 + sw("hal") = 6 (not at the line end)
    _draw(necro, app, monkeypatch, 80, 24)
    out = _draw(necro, app, monkeypatch, 100, 30)  # grow: input row 21 -> 27
    # the input line is rewritten at the new row and the cursor must be repositioned
    assert b"\x1b[27;1H\x1b[2K" in out, "input line not rewritten at new row"
    assert b"\x1b[?25l\x1b[27;6H\x1b[?25h" in out, "cursor not repositioned to col 6"
