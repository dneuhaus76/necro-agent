"""Mouse wheel scrolls the content area (never the prompt history): the terminal
reports wheel notches as SGR mouse events (1000+1006), which feed() must parse
instead of letting them leak into the input buffer as text."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.ui.keys import KeyHandler  # noqa: E402

UP = "\x1b[<64;10;5M"
DOWN = "\x1b[<65;10;5M"


class FakeApp(KeyHandler):
    def __init__(self):
        self.history, self._hist_i = ["old", "newest"], 2
        self.buf, self.cur, self.rx, self.rx_time = "", 0, "", 0.0
        self.scroll = 0
        self.menu, self.menu_sel = [], 0
        self.tab_hint, self._tab_cands, self._tab_i, self._tab_cycle = "", None, 0, None

    def ins(self, s):
        self.buf += s
        self.cur = len(self.buf)


def test_wheel_up_scrolls_content_back():
    k = FakeApp()
    k.feed(UP)
    assert k.scroll == k.WHEEL_LINES
    assert k.buf == ""  # nothing leaked into the prompt


def test_wheel_down_returns_to_present_and_stops_at_zero():
    k = FakeApp()
    k.feed(UP * 2)
    assert k.scroll == 2 * k.WHEEL_LINES
    k.feed(DOWN * 3)
    assert k.scroll == 0  # clamped, no negative scroll


def test_wheel_does_not_touch_history():
    k = FakeApp()
    k.feed(UP)
    assert k.buf == "" and k._hist_i == 2


def test_wheel_with_modifier_bits_still_detected():
    k = FakeApp()
    k.feed("\x1b[<68;1;1M")  # shift + wheel up
    assert k.scroll == k.WHEEL_LINES


def test_click_is_ignored():
    k = FakeApp()
    k.feed("\x1b[<0;4;4M\x1b[<0;4;4m")
    assert k.scroll == 0 and k.buf == ""


def test_split_report_survives_fragmentation():
    """the report may arrive in pieces: the parser must wait, not eat the ESC."""
    k = FakeApp()
    k.feed("\x1b[<64;1")
    assert k.buf == ""
    k.feed("0;5M")
    assert k.scroll == k.WHEEL_LINES


def test_x10_wheel_fallback():
    k = FakeApp()
    k.feed("\x1b[M" + chr(64 + 32) + chr(10) + chr(5))  # legacy encoding, wheel up
    assert k.scroll == k.WHEEL_LINES
