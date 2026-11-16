"""Scroll + incremental draw: the _last_view cache must be refreshed on EVERY
draw, not only when the input line changes. With a stale cache, a row whose NEW
content equals the OLD cached value (e.g. a blank line after scrolling back) is
skipped - stale text stays on screen ("compressed" content, missing blank rows)."""
import os

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


def _draw(necro, app, monkeypatch, w, h):
    monkeypatch.setattr(necro.shutil, "get_terminal_size", lambda *a: os.terminal_size((w, h)))
    out = []
    monkeypatch.setattr(necro.os, "write", lambda fd, data: out.append(data))
    app.dirty = True
    app.draw()
    return b"".join(out)


def _app(necro, pairs=1):
    app = necro.App()
    for i in range(pairs):
        app.add("user", f"frage {i}")
        app.add("agent", f"antwort {i}")
    return app


def test_scroll_back_rewrites_rows(env, monkeypatch):
    """80x24 -> ch=15, 5 pairs -> tot=19 lines. Scroll down by 1 (every content
    row shifts), then back: every row's new content equals the value the STALE
    cache still holds from the first draw - with the bug all rows are skipped
    and the shifted content stays on screen ("compressed", missing blank rows)."""
    necro = env
    app = _app(necro, pairs=5)
    _draw(necro, app, monkeypatch, 80, 24)
    app.scroll = 1
    _draw(necro, app, monkeypatch, 80, 24)  # content shifts up by 1 (input line unchanged)
    app.scroll = 0
    out = _draw(necro, app, monkeypatch, 80, 24)  # content shifts back down by 1
    assert b"\x1b[4;1H\x1b[2K" in out, "row 4 not rewritten after scrolling back (stale _last_view cache)"


def test_wheel_scroll_roundtrip(env, monkeypatch):
    """Same via the wheel handler path (scroll clamped in draw): down 3, up 3.
    5 pairs -> tot=19 lines, ch=15 -> max scroll 4, so scroll=3 is effective."""
    necro = env
    app = _app(necro, pairs=5)
    _draw(necro, app, monkeypatch, 80, 24)
    app.scroll = 3
    _draw(necro, app, monkeypatch, 80, 24)
    app.scroll = 0
    out = _draw(necro, app, monkeypatch, 80, 24)
    assert b"\x1b[4;1H\x1b[2K" in out and b"\x1b[6;1H\x1b[2K" in out, \
        "content rows not rewritten after scrolling back"
