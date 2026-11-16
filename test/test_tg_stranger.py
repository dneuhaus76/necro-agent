"""Telegram stranger routing (whitelist -> stranger_q, alert + 'f u')."""
import threading
import time

from modules.io.tg import TelegramBot


def _bot(whitelist):
    b = TelegramBot("tok", whitelist=whitelist)
    b._loop = lambda: None  # no polling thread in tests (started in __init__)
    return b


def test_route_whitelisted_goes_to_prompt_queue():
    b = _bot(["111"])
    b._route({"update_id": 1, "message": {
        "text": "hi", "chat": {"id": 111}, "from": {"username": "me"}}})
    assert b.q == [("hi", 111, "me")]
    assert b.stranger() is None


def test_route_stranger_goes_to_stranger_queue():
    b = _bot(["111"])
    b._route({"update_id": 2, "message": {
        "text": "hello?", "chat": {"id": 999}, "from": {"first_name": "Bob"}}})
    assert b.q == []
    assert b.stranger() == (999, "Bob")
    assert b.stranger() is None  # popped


def test_route_empty_whitelist_denies_everyone():
    """Security: empty whitelist = nobody may chat (deny-all)."""
    b = _bot([])
    b._route({"update_id": 3, "message": {
        "text": "x", "chat": {"id": 42}, "from": {"first_name": "Ann"}}})
    assert b.q == []
    assert b.stranger() == (42, "Ann")  # routed as stranger, not a prompt


def test_route_duplicate_update_ignored():
    b = _bot(["111"])
    u = {"update_id": 7, "message": {
        "text": "hi", "chat": {"id": 111}, "from": {"username": "me"}}}
    b._route(u)
    b._route(u)
    assert len(b.q) == 1


def test_route_non_text_ignored():
    b = _bot(["111"])
    b._route({"update_id": 8, "message": {"chat": {"id": 111}}})
    assert b.q == [] and b.stranger() is None


def test_poll_tg_stranger_alert(monkeypatch):
    """poll_tg: stranger -> 'you are not permitted in this chat' to sender + alert to my chats."""
    import necro
    app = necro.App.__new__(necro.App)  # no __init__ (needs a TTY)
    sent = []
    tg = _bot(["111"])
    tg.stranger_q.append((999, "Bob"))
    tg.send = lambda cid, text: sent.append((cid, text))
    app.tg, app.tg_chats, app.dirty = tg, ["111"], False
    app.poll_tg()
    assert (999, "you are not permitted in this chat") in sent
    assert any(cid == "111" and "999" in t and "Bob" in t for cid, t in sent)
    assert app.dirty is True


def test_poll_tg_no_tg_is_noop(monkeypatch):
    import necro
    app = necro.App.__new__(necro.App)
    app.tg, app.tg_chats = None, []
    app.poll_tg()  # must not raise


# ---------------------------------------------------------------------------
# N173 (reverted): '/' is a plain character again; popup close stays generic
# ---------------------------------------------------------------------------
def _app():
    import necro
    app = necro.App.__new__(necro.App)
    app.buf, app.cur, app.menu, app.menu_sel, app.dirty = "", 0, None, 0, False
    app._tab_cycle, app._tab_cands, app._tab_i = None, None, 0
    app.busy, app.tab_hint = False, ""
    return app


def test_slash_is_plain_character():
    app = _app()
    app.key("/")
    assert app.buf == "/" and app.menu is None


def test_escape_closes_popup():
    app = _app()
    app.menu, app.menu_sel = ["a", "b"], 1
    app.key("\x1b")
    assert app.menu is None and app._tab_cycle is None


def test_typing_closes_popup():
    app = _app()
    app.menu = ["a", "b"]
    app.key("x")
    assert app.menu is None and app.buf == "x"


# ---------------------------------------------------------------------------
# TAB completion for the filesystem (files + dirs, relative to cwd)
# ---------------------------------------------------------------------------
def test_dot_slash_lists_directory():
    import os
    app = _app()
    app.key("."), app.key("/")
    app.key("\t")
    assert app.buf.startswith("./")
    assert app.menu is not None  # popup with the directory entries
    assert "plan" in app.menu and "test" in app.menu
    assert os.path.isdir("plan") and os.path.isdir("test")


def test_dir_completion_gets_trailing_slash():
    app = _app()
    app.key("pl"), app.key("\t")
    assert app.buf == "plan/"  # unique directory: insert + '/'


def test_file_completion_no_trailing_slash():
    import os
    app = _app()
    app.key("AGENTS.md"), app.key("\t")
    assert app.buf == "AGENTS.md" and app.menu is None
    assert os.path.isfile("AGENTS.md")


def test_file_no_match_popup():
    app = _app()
    app.key("zzz-no-such-file"), app.key("\t")
    assert app.buf == "zzz-no-such-file"
    assert app.menu == ["(no matches)"]


def test_slash_command_still_completes():
    app = _app()
    app.key("/q")
    app.key("\t")
    assert app.buf == "/quit " and app.menu is None  # single candidate + space


# ---------------------------------------------------------------------------
# /help: TUI key + command reference
# ---------------------------------------------------------------------------
def test_help_shows_reference():
    import necro
    app = _app()
    app.busy, app._queued = False, False
    app.history, app._hist_i = [], 0
    blocks = []
    app.add = lambda kind, text: blocks.append((kind, text))
    app.submit("/help")
    assert blocks == [("info", necro.TUI_HELP)]


def test_help_in_tab_completion():
    import necro
    app = _app()
    app.key("/h")
    app.key("\t")
    assert app.buf == "/help " and app.menu is None  # single candidate + space


# ---------------------------------------------------------------------------
# removed TUI keys (^a/^e/^w/^k/^u + F12 mouse) no longer act
# ---------------------------------------------------------------------------
def test_removed_cursor_keys_no_longer_act():
    app = _app()
    for c in "abcd":
        app.key(c)
    app.key("\x01")  # ^a was: cursor to start
    assert app.cur == 4  # now a no-op (control char ignored), not 0
    app.key("\x05")  # ^e was: cursor to end
    assert app.cur == 4  # still 4 (no-op), not len(buf)


def test_removed_cut_keys_no_longer_act():
    app = _app()
    for c in "hello world":
        app.key(c)
    app.key("\x0b")  # ^k was: cut to end
    assert app.buf == "hello world"  # unchanged
    app.key("\x15")  # ^u was: cut to start
    assert app.buf == "hello world"  # unchanged


def test_toggle_mouse_removed():
    import necro
    assert not hasattr(necro.App, "toggle_mouse")  # F12 mouse mode gone
