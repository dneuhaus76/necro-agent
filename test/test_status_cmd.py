"""'/status' reports consumed tokens - real prompt_tokens when known, otherwise a
clearly labelled estimate - and the same text goes out on Telegram."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import necro  # noqa: E402


def _app():
    """App without the heavy __init__ (sessions/agent/probe): only the state
    status_text reads, plus the block + telegram recording used by the tests."""
    a = object.__new__(necro.App)
    a.ctx, a.ctx_used, a.ctx_est = 65536, 0, 0
    a.ctx_capped_key = None
    a.model, a.turns, a.step, a.busy = "test-model", 0, 0, False
    a.last_completion_tokens = 0
    a.history, a._hist_i, a.buf, a.cur, a.tab_hint, a.menu = [], 0, "", 0, "", None
    a.think, a.show_tools, a.show_edits, a.quit = True, True, True, False
    a.blocks, a.dirty, a._queued, a._queued_text, a._queued_block = [], False, False, "", None
    a.tg_chats, a.peer = [], None
    a.sent = []
    a.tg = type("TG", (), {"send": lambda _self, cid, txt: a.sent.append((cid, txt))})()
    a.add = lambda kind, text="", color=None: a.blocks.append((kind, text))
    a.clear_session = lambda: None
    return a


def test_real_tokens_shown_as_real():
    a = _app()
    a.ctx_used = 12345
    a.last_completion_tokens = 678
    txt = a.status_text()
    assert "12'345" in txt and "65'536" in txt
    assert "real" in txt and "geschaetzt" not in txt
    assert "678" in txt  # last reply tokens
    assert "18.8%" in txt


def test_estimate_is_labelled():
    a = _app()
    a.ctx_est = 1000
    txt = a.status_text()
    assert "1'000" in txt
    assert "geschaetzt" in txt


def test_status_reports_free_space_and_state():
    a = _app()
    a.ctx_used, a.busy, a.step, a.turns = 63536, True, 3, 7
    txt = a.status_text()
    assert "frei: 2'000 tokens" in txt
    assert "busy (step 3)" in txt and "turn 7" in txt


def test_tui_submit_dispatches_status_command():
    a = _app()
    a.ctx_used = 4096
    a.submit("/status")
    assert a.blocks and a.blocks[-1][0] == "info"
    assert "tokens:" in a.blocks[-1][1]
    assert a.quit is False  # /status is not a turn and does not quit


def test_telegram_status_uses_same_text(monkeypatch):
    monkeypatch.setattr(necro, "TG_CHATS", ["42"])  # chat 42 is whitelisted
    a = _app()
    a.ctx_used = 2048
    a.tg_submit("/status", "42")
    assert a.sent and a.sent[-1][0] == "42"
    assert a.sent[-1][1] == a.status_text()
    assert "tokens:" in a.sent[-1][1]


def test_num_uses_de_ch_separator():
    assert necro.num(1234567) == "1'234'567"
    assert necro.num(0) == "0"


def test_status_answers_immediately_while_busy():
    """/status is a read-only query: it must not queue behind a running turn."""
    a = _app()
    a.busy, a.ctx_used = True, 1024
    a.abort = lambda: None
    a.submit("/status")
    assert a.blocks and a.blocks[-1][0] == "info"
    assert "tokens:" in a.blocks[-1][1]
    assert a._queued is False and a._queued_text == ""  # nothing queued


def test_help_answers_immediately_while_busy():
    a = _app()
    a.busy = True
    a.submit("/help")
    assert a.blocks[-1][1] == necro.TUI_HELP
    assert a._queued is False


def test_plain_prompt_still_queues_while_busy():
    a = _app()
    a.busy = True
    a.submit("do something")
    assert a._queued is True and a._queued_text == "do something"


def test_quit_aborts_a_running_turn():
    a = _app()
    a.busy, a.aborted = True, []
    a.abort = lambda: a.aborted.append(True)
    a.submit("/quit")
    assert a.aborted == [True]
    assert a.quit is True


def test_telegram_quit_aborts_a_running_turn(monkeypatch):
    monkeypatch.setattr(necro, "TG_CHATS", ["42"])
    a = _app()
    a.busy, a.aborted = True, []
    a.abort = lambda: a.aborted.append(True)
    a.tg_submit("/quit", "42")
    assert a.aborted == [True] and a.quit is True
