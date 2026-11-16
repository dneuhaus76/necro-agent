"""recall renders like the thinking line (own block kind) instead of a
generic tool block - head with query + hit count, body behind the ^t toggle."""
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


def _plain(lines):
    import re
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", l) for l in lines)


def _app(necro, think=True):
    app = necro.App()
    app.think = think
    app.show_tools = False  # the ^o toggle must NOT control recall any more
    return app


def _hits():
    return "[1] user: frage nach dem plan\n[2] agent: antwort zum plan"


def test_recall_block_has_own_kind(env):
    """The Agent must create kind 'recall', not 'tool' - the UI branches on it."""
    from modules.core.agent import Agent

    assert Agent._kind({"name": "recall"}) == "recall"
    assert Agent._kind({"name": "bash"}) == "tool"


def test_recall_head_shows_query_and_hit_count(env):
    app = _app(env)
    out = _plain(app.render(env.Block("recall", "recall  plan\n" + _hits()), 80))
    head = out.split("\n")[0]
    assert "recall plan" in head
    assert "2 hits" in head
    assert "Ctrl-t" not in head


def test_recall_singular_hit_and_no_matches(env):
    app = _app(env)
    assert "1 hit" in _plain(app.render(env.Block("recall", "recall  x\n[1] user: x"), 80)).split("\n")[0]
    zero = _plain(app.render(env.Block("recall", "recall  y\nno matches in past sessions"), 80))
    assert "0 hits" in zero.split("\n")[0]
    assert "no matches" not in zero.split("\n")[0]  # the placeholder is not a hit line


def test_recall_body_visible_when_thinking_on(env):
    app = _app(env, think=True)
    out = _plain(app.render(env.Block("recall", "recall  plan\n" + _hits()), 80))
    assert "frage nach dem plan" in out and "antwort zum plan" in out
    assert "┊" in out  # same gutter as the thinking body


def test_recall_body_hidden_without_thinking(env):
    app = _app(env, think=False)
    out = _plain(app.render(env.Block("recall", "recall  plan\n" + _hits()), 80))
    assert "frage nach dem plan" not in out
    assert "Ctrl-t to show" in out  # toggle hint, like the think line
    assert "2 hits" in out  # the head stays informative


def test_recall_body_independent_of_tools_toggle(env):
    """showing recall must not depend on ^o (show_tools) - that was the N244 bug."""
    app = _app(env, think=True)
    app.show_tools = False
    assert "frage nach dem plan" in _plain(app.render(env.Block("recall", "recall  p\n" + _hits()), 80))
    app.think, app.show_tools = False, True  # other direction: tools on does not reveal it
    assert "frage nach dem plan" not in _plain(app.render(env.Block("recall", "recall  p\n" + _hits()), 80))


def test_toggle_rerenders_recall(env):
    """The render cache key must include the thinking state (else the body sticks."""
    app = _app(env, think=True)
    blk = env.Block("recall", "recall  p\n" + _hits())
    assert "frage nach dem plan" in _plain(app.render(blk, 80))
    app.think = False
    assert "frage nach dem plan" not in _plain(app.render(blk, 80))
