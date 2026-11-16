"""while the agent writes, the physical cursor must stay on the input line.
After ANY write the terminal cursor sits at the end of the LAST written row - with a
busy agent that is a content row. The draw must reposition the cursor to the input
line unless the input row was the last write AND the wanted column is its end."""
import os

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


def _draw(necro, app, monkeypatch, w, h):
    monkeypatch.setattr(necro.shutil, "get_terminal_size", lambda *a: os.terminal_size((w, h)))
    monkeypatch.setattr(necro.time, "time", lambda: 1000.0)  # freeze spinner + cwd flash
    out = []
    monkeypatch.setattr(necro.os, "write", lambda fd, data: out.append(data))
    app.dirty = True
    app.draw()
    return b"".join(out)


def test_cursor_stays_on_input_while_agent_writes(env, monkeypatch):
    """80x24 -> ch=15, y=ch+4=19, input row = y+2 = 21. The agent block grows
    between draws: content rows are rewritten, the input row is cached (unchanged)
    - the physical cursor ends at a content row and must be moved back to col 5
    of row 21."""
    necro = env
    app = necro.App()
    app.add("user", "frage")
    app.add("agent", "antwort")
    app.busy = True
    app.buf, app.cur = "hi", 2
    _draw(necro, app, monkeypatch, 80, 24)  # first draw: input row written last, cursor at its end
    app.blocks[-1].text = "antwort wird laenger und laenger"  # the agent keeps writing
    out = _draw(necro, app, monkeypatch, 80, 24)
    assert b"\x1b[?25l\x1b[21;5H\x1b[?25h" in out, \
        "cursor not repositioned to the input line while the agent writes"


def test_idle_draw_no_change_no_cursor_move(env, monkeypatch):
    """Nothing changed since the last draw: no write at all, the cursor stays put."""
    necro = env
    app = necro.App()
    app.add("user", "frage")
    app.buf, app.cur = "hi", 2
    _draw(necro, app, monkeypatch, 80, 24)
    out = _draw(necro, app, monkeypatch, 80, 24)
    assert out == b""


def test_arrow_keys_move_cursor_without_write(env, monkeypatch):
    """Left/Right only change cur - the input line content is unchanged, so no
    row is rewritten (f empty). The physical cursor still must follow the wanted
    position (it sat at the line end after the previous draw)."""
    necro = env
    app = necro.App()
    app.add("user", "frage")
    app.buf, app.cur = "hello", 5
    _draw(necro, app, monkeypatch, 80, 24)  # cursor at line end (col 8)
    app.cur = 3  # Left Left: no content change (col = 1 + sw("› he") = 6)
    out = _draw(necro, app, monkeypatch, 80, 24)
    assert b"\x1b[?25l\x1b[21;6H\x1b[?25h" in out, \
        "cursor not moved to the wanted column when no row was rewritten"
    app.cur = 4  # Right: back to col 7
    out = _draw(necro, app, monkeypatch, 80, 24)
    assert b"\x1b[?25l\x1b[21;7H\x1b[?25h" in out, \
        "cursor not moved back to the right"
