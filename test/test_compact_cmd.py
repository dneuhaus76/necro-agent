"""/compact - shrink the context on demand (TUI and Telegram).

The compaction itself existed as an automatic step (Agent.compact, triggered at
80 % of the window) but there was no way to ask for it: a user who wants to free
context NOW had to wait for the threshold. `compact(force=True)` skips the
threshold, returns a one-line report, and both frontends share
App.compact_command() - which runs in a thread, because the summary is one LLM
call (the TUI and the Telegram poller are single-threaded loops).
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import necro  # noqa: E402
from modules.core.agent import Agent  # noqa: E402


class _App:
    """Minimal App for the compaction: messages, ctx, a report list."""

    def __init__(self, messages, ctx=1000):
        self.messages = messages
        self.ctx, self.ctx_used = ctx, 0
        self.stop, self.conns = threading.Event(), []
        self.blocks = []

    def add(self, kind, text="", color=None):
        self.blocks.append((kind, text))

    def db_sync(self):
        pass


def _agent(app, summary="SUMMARY"):
    ag = Agent(app, base="http://local", step_timeout=10, tools_list=[], sub_tools=[],
               swarm=[], swarm_up={}, agents_md="", agents_md_path="")
    ag.complete = lambda msgs, base=None, model=None: summary  # no server
    return ag


def _history(n):
    """system + n user/assistant pairs - enough to have a summarisable head."""
    out = [{"role": "system", "content": "rules"}]
    for i in range(n):
        out.append({"role": "user", "content": f"frage {i}"})
        out.append({"role": "assistant", "content": f"antwort {i}"})
    return out


def test_auto_compact_below_the_threshold_does_nothing():
    app = _App(_history(5))
    ag = _agent(app)
    before = list(app.messages)
    assert ag.compact() == ""
    assert app.messages == before


def test_forced_compact_summarises_the_head():
    app = _App(_history(5))
    ag = _agent(app, "ZUSAMMENFASSUNG")
    report = ag.compact(force=True)
    assert report.startswith("compact: 11 messages -> 1 summary + 6 recent")
    assert app.messages[0]["content"] == "rules"  # system prompt survives
    assert app.messages[1]["content"].startswith("[Compacted context]\nZUSAMMENFASSUNG")
    assert len(app.messages) == 1 + 1 + 6  # system + summary + the last 6 messages


def test_forced_compact_on_a_short_context_reports_nothing_to_do():
    """Only system + a few messages: no head to summarise - do not spend a call."""
    app = _App(_history(2))
    ag = _agent(app)
    calls = []
    ag.complete = lambda *a, **kw: calls.append(1) or "x"
    assert ag.compact(force=True) == ""
    assert calls == [], "a short context must not cost an LLM call"


def test_forced_compact_keeps_a_tool_call_pair_together():
    m = _history(4)
    m.insert(-2, {"role": "assistant", "content": None,
                  "tool_calls": [{"id": "1", "type": "function",
                                  "function": {"name": "bash", "arguments": "{}"}}]})
    m.insert(-1, {"role": "tool", "tool_call_id": "1", "content": "ausgabe"})
    app = _App(m)
    ag = _agent(app)
    ag.compact(force=True)
    roles = [x["role"] for x in app.messages]
    # the surviving tail never starts with an orphaned tool result
    assert roles[2] != "tool", "tail must not start with an orphaned tool result"


def test_forced_compact_trims_huge_tool_results():
    m = _history(4) + [{"role": "assistant", "content": None,
                        "tool_calls": [{"id": "1", "type": "function",
                                        "function": {"name": "bash", "arguments": "{}"}}]},
                       {"role": "tool", "tool_call_id": "1", "content": "x" * 9000}]
    app = _App(m)
    ag = _agent(app)
    report = ag.compact(force=True)
    assert "tool results trimmed" in report
    assert any("[trimmed, 9000 chars total]" in (x.get("content") or "") for x in app.messages)


# ---- frontends -----------------------------------------------------------
def _ui_app(monkeypatch, summary="SUM"):
    """/compact from the TUI: real App, agent stubbed, thread waited for."""
    monkeypatch.setattr(necro, "_resolve_model_info", lambda app: None)
    app = necro.App.__new__(necro.App)
    app.blocks, app.sent = [], []
    app.add = lambda kind, text="", color=None: app.blocks.append((kind, text))
    app.busy = False
    app.agent = type("A", (), {"compact": lambda _self, force=False: summary})()
    app.tg = type("TG", (), {"send": lambda _self, cid, txt: app.sent.append((cid, txt))})()
    return app


def _wait_blocks(app, n=1, timeout=2.0):
    end = time.monotonic() + timeout
    while len(app.blocks) < n and time.monotonic() < end:
        time.sleep(0.01)
    return app.blocks


def test_tui_compact_reports_the_result(monkeypatch):
    app = _ui_app(monkeypatch, "compact: 11 messages -> 1 summary + 6 recent")
    app.compact_command()
    assert _wait_blocks(app) == [("info", "compact: 11 messages -> 1 summary + 6 recent")]


def test_tui_compact_nothing_to_do(monkeypatch):
    app = _ui_app(monkeypatch, "")
    app.compact_command()
    assert _wait_blocks(app) == [("info", "compact: nothing to do (context is small)")]


def test_compact_error_is_reported_not_raised(monkeypatch):
    app = _ui_app(monkeypatch)
    app.agent = type("A", (), {"compact": lambda _self, force=False: (_ for _ in ()).throw(
        OSError("server weg"))})()
    app.compact_command()
    assert _wait_blocks(app)[0][1] == "compact failed: OSError: server weg"


def test_tg_compact_sends_to_the_chat(necro_app, monkeypatch):
    app = necro_app
    app.agent = type("A", (), {"compact": lambda _self, force=False: "compact: ok"})()
    app.tg_submit("/compact", 111, "me")
    end = time.monotonic() + 2
    while not app.sent and time.monotonic() < end:
        time.sleep(0.01)
    assert app.sent == [(111, "compact: ok")]


def test_tg_compact_queues_while_busy(necro_app):
    """A compaction rewrites app.messages - it must never race a running turn."""
    app = necro_app
    app.busy = True
    app.tg_submit("/compact", 111, "me")
    assert app._tg_queued == ("/compact", 111, "me")
    assert app.sent == [(111, "(queued - runs after the current turn)")]


def test_compact_is_listed_in_menu_and_help():
    names = [n for n, _ in necro.TG_COMMAND_SPECS]
    assert "compact" in names
    assert "/compact" in necro.TG_COMMANDS
    assert "/compact" in necro.HELP_TUI
    assert "/compact" in necro.TG_KNOWN


def test_compact_is_not_counted_as_a_turn():
    assert necro.App._is_turn_text("/compact") is False


@pytest.fixture
def necro_app(monkeypatch, tmp_path):
    """Real App (no server, no DB writes outside tmp), Telegram stubbed."""
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    monkeypatch.setattr(necro, "TG_CHATS", ["111"])
    monkeypatch.setattr(necro, "_resolve_model_info", lambda app: None)
    app = necro.App()
    app.sent, app.busy = [], False
    app._tg_queued = None
    app.tg = type("TG", (), {
        "send": lambda _self, cid, txt: app.sent.append((cid, txt)),
        "set_my_commands": lambda _self, specs: True,
        "clear_my_commands": lambda _self: True,
    })()
    yield app
    app.sessions.close()
