"""Telegram bot command menu: setMyCommands on startup, /tgcommands update/clear."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import necro  # noqa: E402
from modules.io.tg import TelegramBot  # noqa: E402


def _bot():
    b = TelegramBot("tok")
    b._loop = lambda: None  # no polling thread in tests
    b.calls = []
    b._api = lambda method, **params: b.calls.append((method, params)) or {"ok": True}
    return b


def test_set_my_commands_pushes_spec():
    b = _bot()
    assert b.set_my_commands(necro.TG_COMMAND_SPECS) is True
    method, params = b.calls[0]
    assert method == "setMyCommands"
    names = [c["command"] for c in params["commands"]]
    assert names == [name for name, _ in necro.TG_COMMAND_SPECS]


def test_set_my_commands_truncates_description():
    b = _bot()
    b.set_my_commands([("x", "y" * 100)])
    assert b.calls[0][1]["commands"][0]["description"] == "y" * 25  # Bot API limit


def test_clear_my_commands_sends_empty_list():
    b = _bot()
    assert b.clear_my_commands() is True
    assert b.calls[0] == ("setMyCommands", {"commands": []})


def test_set_my_commands_failure_returns_false():
    b = _bot()
    b._api = lambda method, **params: {"ok": False, "description": "bad token"}
    assert b.set_my_commands(necro.TG_COMMAND_SPECS) is False


def test_help_text_derived_from_specs():
    for name, desc in necro.TG_COMMAND_SPECS:
        assert f"/{name}" in necro.TG_COMMANDS
        assert desc in necro.TG_COMMANDS
    assert "/tgcommands" in necro.TG_COMMANDS


def _app(monkeypatch):
    monkeypatch.setattr(necro, "TG_CHATS", ["111"])  # chat 111 is whitelisted
    app = necro.App.__new__(necro.App)
    app.busy, app.quit = False, False
    app.sent = []
    app.tg = type("TG", (), {
        "send": lambda _self, cid, txt: app.sent.append((cid, txt)),
        "set_my_commands": lambda _self, specs: app.set_ok,
        "clear_my_commands": lambda _self: app.set_ok,
    })()
    app.set_ok = True
    return app


def test_tgcommands_update(monkeypatch):
    app = _app(monkeypatch)
    app.tg_submit("/tgcommands", 111, "me")
    assert app.sent == [(111, f"tgcommands: menu updated ({len(necro.TG_COMMAND_SPECS)} commands)")]


def test_tgcommands_clear(monkeypatch):
    app = _app(monkeypatch)
    app.tg_submit("/tgcommands clear", 111, "me")
    assert app.sent == [(111, "tgcommands: menu cleared")]


def test_tgcommands_failure_message(monkeypatch):
    app = _app(monkeypatch)
    app.set_ok = False
    app.tg_submit("/tgcommands", 111, "me")
    assert app.sent == [(111, "tgcommands: setMyCommands failed (check the token)")]
