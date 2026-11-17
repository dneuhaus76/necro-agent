"""error results ("error: ...") render in red instead of the normal block color.

The convention lives in core.agent.run_tool: a failing tool returns a string that
starts with "error:". The UI detects that (render.is_err) and paints it red -
in tool bodies, in the collapsed head hint and in info blocks.
"""
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


ERROR = ";38;5;167m"  # palette "error" = 167 (see core.palette) - as an SGR suffix,
                    # it overrides the block base color (dark/grey/...)
ERR_TEXT = "error: old text found 0x (exactly 1 required)"


def _plain(lines):
    import re
    return [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in lines]


def _app(necro, tools=True, edits=False):
    app = necro.App()
    app.show_tools, app.show_edits = tools, edits
    return app


def test_is_err_detects_convention():
    from modules.ui.render import is_err

    assert is_err(ERR_TEXT)
    assert is_err("  Error: boom")  # leading space / case are tolerated
    assert not is_err("error code 3")  # not the "error:" convention
    assert not is_err("")
    assert not is_err(None)


def test_sgr_error_flag_is_red_on_any_base():
    from modules.ui.render import MUTED, sgr

    assert ERROR in sgr(MUTED, "e")
    assert ERROR not in sgr(MUTED, "")  # no stray red without the flag
    assert ERROR in sgr(MUTED, "be") and ";1" in sgr(MUTED, "be")  # bold + red


def test_tool_error_body_is_red(env):
    app = _app(env, edits=True)
    blk = env.Block("tool", "edit_file  a.py\n" + ERR_TEXT)
    lines = app.render(blk, 80)
    body = lines[1:]
    assert ERR_TEXT in _plain(body)[0]
    assert all(ERROR in l for l in body), lines


def test_tool_success_body_stays_dark(env):
    from modules.ui.render import MUTED

    app = _app(env)
    lines = app.render(env.Block("tool", "bash  ls\na.py\nb.py"), 80)
    assert ERROR not in "".join(lines)
    assert all(MUTED in l for l in lines[1:])


def test_edit_error_body_is_red_not_diff_colored(env):
    """A failing edit has no diff to color - its "error:" line must be red."""
    app = _app(env, tools=False, edits=True)
    lines = app.render(env.Block("tool", "edit_file  a.py\n" + ERR_TEXT), 80)
    assert ERROR in lines[1]
    assert ";48;5;216" not in lines[1] and ";48;5;106" not in lines[1]  # no edit del/add bg


def test_collapsed_error_head_signals_failure(env):
    """With the body hidden the failure must still be visible: '· error' in red."""
    app = _app(env, tools=False)
    lines = app.render(env.Block("tool", "edit_file  a.py\n" + ERR_TEXT), 80)
    text = _plain(lines)[0]
    assert "(Ctrl-x to show)" in text and "· error" in text
    assert ERROR in lines[0] and ";3" in lines[0]  # red + italic hint


def test_collapsed_head_without_error_has_no_marker(env):
    app = _app(env, tools=False)
    text = _plain(app.render(env.Block("tool", "bash  ls\na.py"), 80))[0]
    assert "· error" not in text


def test_info_block_with_error_text_is_red(env):
    from modules.ui.render import MUTED

    app = _app(env)
    assert ERROR in app.render(env.Block("info", ERR_TEXT), 80)[0]
    assert MUTED in app.render(env.Block("info", "cd: /tmp"), 80)[0]


def test_explicit_block_color_wins_over_red(env):
    """b.color is an explicit override (e.g. telegram blue) - it stays untouched."""
    from modules.ui.render import TELEGRAM

    app = _app(env)
    assert TELEGRAM in app.render(env.Block("info", ERR_TEXT, color=TELEGRAM), 80)[0]
