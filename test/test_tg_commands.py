"""Telegram bot command menu: setMyCommands on startup, /tgcommands update|remove,
and the commands the Telegram side answers itself (/model, /status, ...).

The menu is an SSOT list (TG_COMMAND_SPECS): it feeds the client's '/' menu AND
the /help text - a command that is only in the help text never shows up in the
Telegram client, which is exactly how /tgcommands and /model were missing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import necro  # noqa: E402
from modules.io import tg  # noqa: E402
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


def test_set_my_commands_keeps_the_description():
    """The old code cut every description at 25 chars, so the menu showed
    'interrupt the current tur'. The Bot API allows 256 - send the real text."""
    b = _bot()
    b.set_my_commands([("x", "y" * 100)])
    assert b.calls[0][1]["commands"][0]["description"] == "y" * 100


def test_set_my_commands_clips_only_at_the_api_limit():
    b = _bot()
    b.set_my_commands([("x", "y" * 300)])
    assert len(b.calls[0][1]["commands"][0]["description"]) == tg.TG_DESC_MAX


def test_set_my_commands_drops_an_invalid_name():
    """One bad name makes Telegram reject the whole menu - skip it, keep the rest."""
    b = _bot()
    assert b.set_my_commands([("Bad Name", "d"), ("ok", "d")]) is True
    assert [c["command"] for c in b.calls[0][1]["commands"]] == ["ok"]


def test_descriptions_are_short_and_never_cut_mid_word():
    """The client column is narrow: keep the text short, but never truncated.
    A description that ends inside a word is what the user saw as 'abgeschnitten'."""
    for name, desc in necro.TG_COMMAND_SPECS:
        assert desc and desc == desc.strip()
        assert len(desc) <= 32, f"/{name}: {desc!r} is {len(desc)} chars - the client column cuts it"
        assert desc[-1] not in "-/", f"/{name}: {desc!r} ends in a separator"
        assert desc.split()[-1].isalpha(), f"/{name}: {desc!r} ends mid-word"


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


def test_menu_contains_every_answered_command():
    """SSOT check: what tg_submit() answers must be in the client menu."""
    names = {name for name, _ in necro.TG_COMMAND_SPECS}
    assert {"stop", "new", "model", "status", "tgcommands", "help", "quit"} <= names


def test_tg_command_splits_arguments_and_bot_suffix():
    assert necro.tg_command("/model") == ("/model", "")
    assert necro.tg_command("/model Qwen3.8-27B") == ("/model", "Qwen3.8-27B")
    assert necro.tg_command("/model@necro_bot  x ") == ("/model", "x")
    assert necro.tg_command("/STATUS") == ("/status", "")
    assert necro.tg_command("normal prompt") == ("", "normal prompt")
    assert necro.tg_command("/bogus x") == ("/bogus", "x")  # unknown: answered, not run


# ---- App side ------------------------------------------------------------
def _app(monkeypatch):
    monkeypatch.setattr(necro, "TG_CHATS", ["111"])  # chat 111 is whitelisted
    monkeypatch.setattr(necro, "_resolve_model_info", lambda app: None)  # no network probe
    app = necro.App.__new__(necro.App)
    app.busy, app.quit = False, False
    app.model, app.ctx_raw, app.ctx_capped_key = "Qwen3.8-27B-Swift", 131072, None
    app.model_info = necro.probe.default_info(131072)
    app.peer = None
    app.sent, app.blocks = [], []
    app.history, app._hist_i, app._queued = [], 0, False
    app.buf, app.cur, app.tab_hint, app.menu = "", 0, "", None
    app.add = lambda kind, text="", color=None: app.blocks.append((kind, text))
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


def test_tgcommands_update_and_remove_words(monkeypatch):
    app = _app(monkeypatch)
    app.tg_submit("/tgcommands update", 111, "me")
    app.tg_submit("/tgcommands remove", 111, "me")
    app.tg_submit("/tgcommands clear", 111, "me")  # legacy word, same effect
    assert [t for _, t in app.sent] == [
        f"tgcommands: menu updated ({len(necro.TG_COMMAND_SPECS)} commands)",
        "tgcommands: menu cleared",
        "tgcommands: menu cleared",
    ]


def test_tgcommands_bot_suffix(monkeypatch):
    """Telegram appends '@botname' in groups - that must still hit the command."""
    app = _app(monkeypatch)
    app.tg_submit("/tgcommands@necro_bot update", 111, "me")
    assert app.sent == [(111, f"tgcommands: menu updated ({len(necro.TG_COMMAND_SPECS)} commands)")]


def test_tgcommands_unknown_sub_command(monkeypatch):
    app = _app(monkeypatch)
    app.tg_submit("/tgcommands foo", 111, "me")
    text = app.sent[0][1]
    assert "unknown 'foo'" in text and "/tgcommands remove" in text


def test_tgcommands_failure_message(monkeypatch):
    app = _app(monkeypatch)
    app.set_ok = False
    app.tg_submit("/tgcommands", 111, "me")
    assert app.sent == [(111, "tgcommands: setMyCommands failed (check the token)")]


def test_model_without_argument_shows_details(monkeypatch):
    app = _app(monkeypatch)
    app.tg_submit("/model", 111, "me")
    text = app.sent[0][1]
    assert text.startswith("ID              : Qwen3.8-27B-Swift")


def test_model_switches(monkeypatch):
    app = _app(monkeypatch)
    app.tg_submit("/model Ornith1.5-35B", 111, "me")
    assert app.model == "Ornith1.5-35B"
    assert app.sent == [(111, "model: Ornith1.5-35B (probe laeuft …)")]


def test_unknown_command_is_answered_not_run(monkeypatch):
    """A typo used to start an agent turn (costs tokens, confuses the user)."""
    app = _app(monkeypatch)
    app.tg_submit("/modle", 111, "me")
    text = app.sent[0][1]
    assert text.startswith("unknown command /modle") and "Commands from Telegram" in text


def test_tui_tgcommands_shares_the_implementation(monkeypatch):
    """The TUI answers /tgcommands with the same function (one message text)."""
    app = _app(monkeypatch)
    app.submit("/tgcommands update")
    assert app.blocks == [("info", f"tgcommands: menu updated "
                                   f"({len(necro.TG_COMMAND_SPECS)} commands)")]
    assert app.tg_menu("remove") == "tgcommands: menu cleared"


def test_tui_tgcommands_without_telegram(monkeypatch):
    app = _app(monkeypatch)
    app.tg = None
    assert app.tg_menu("update").startswith("tgcommands: Telegram is off")
