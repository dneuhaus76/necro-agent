"""Up/Down arrows walk the prompt history ONLY - the content area no longer
scrolls on arrow presses (PgUp/PgDn/wheel do that). Up always starts from the
newest entry, even over a user-typed line."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.ui.keys import KeyHandler  # noqa: E402


class FakeApp(KeyHandler):
    """KeyHandler is a mixin for the App - the test app provides only the state
    the arrow-key paths touch (history, buf, cur, scroll)."""
    def __init__(self):
        self.history = ["old", "newer", "newest"]
        self._hist_i = len(self.history)
        self.buf = ""
        self.cur = 0
        self.scroll = 0
        self.menu = []
        self.menu_sel = 0
        self.tab_hint = ""
        self.dirty = True


def _keys():
    return FakeApp()


def test_up_walks_history_from_newest():
    k = _keys()
    assert k.hist_nav(True) is True
    assert k.buf == "newest"
    assert k.hist_nav(True) is True
    assert k.buf == "newer"
    assert k.hist_nav(True) is True
    assert k.buf == "old"
    assert k.hist_nav(True) is False  # boundary: nothing older


def test_down_walks_back_to_present():
    k = _keys()
    k.hist_nav(True)  # newest
    k.hist_nav(True)  # newer
    assert k.hist_nav(False) is True
    assert k.buf == "newest"
    assert k.hist_nav(False) is True
    assert k.buf == ""  # present (empty buffer)
    assert k.hist_nav(False) is False  # boundary: nothing newer


def test_up_over_user_typed_line_starts_from_newest():
    """the user typed text, then presses Up: the newest entry is loaded (the typed
    line is replaced - it stays editable in the buffer until then)."""
    k = _keys()
    k.buf = "typed text"
    k.cur = len(k.buf)
    assert k.hist_nav(True) is True
    assert k.buf == "newest"


def test_modified_loaded_entry_restarts_walk():
    k = _keys()
    k.hist_nav(True)  # loads "newest"
    k.buf = "newest edited"  # user typed on top of the loaded entry
    assert k.hist_nav(True) is True
    assert k.buf == "newer"  # walk restarted from the newest entry


def test_arrows_do_not_scroll_content():
    """csi A/B: no history -> arrows must not touch self.scroll (content area)."""
    k = _keys()
    k.history = []
    k._hist_i = 0
    k.csi("", "A")
    k.csi("", "B")
    assert k.scroll == 0


def test_arrows_load_history_not_scroll():
    """with history, arrows load entries instead of scrolling the content."""
    k = _keys()
    k.scroll = 5
    k.csi("", "A")
    assert k.buf == "newest"
    assert k.scroll == 5  # content untouched
