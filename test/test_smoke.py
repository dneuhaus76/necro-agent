"""Smoke tests for NECRO – fast, no integration, < 5 s total."""
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# ---------------------------------------------------------------------------
# 1. All modules importable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mod", [
    "modules.core.config",
    "modules.core.context",
    "modules.core.inference",
    "modules.core.palette",
    "modules.core.probe",
    "modules.core.sessiondb",
    "modules.core.templates",
    "modules.core.agent",
    "modules.ui.render",
    "modules.ui.subagent",
    "modules.ui.keys",
    "modules.io.api",
    "modules.io.swarm",
    "modules.io.tg",
    "modules.io.tools",
])
def test_import(mod):
    """Every public module must import without side-effects."""
    __import__(mod)


# ---------------------------------------------------------------------------
# 2. config.validate
# ---------------------------------------------------------------------------
def test_validate_empty():
    """validate({}) returns an empty list (no issues)."""
    from modules.core.config import validate
    result = validate({})
    assert isinstance(result, list)
    assert result == []


def test_validate_type_error():
    """validate({'base_url': 42}) reports a type mismatch."""
    from modules.core.config import validate
    issues = validate({"base_url": 42})
    assert len(issues) >= 1
    assert "expects str" in issues[0] or "got int" in issues[0]


def test_tool_emoji_cfg(monkeypatch):
    """cfg 'tool_emoji' overrides the default tool symbols in the render."""
    import necro
    monkeypatch.setitem(necro.TOOL_EMOJI, "bash", "🔧")
    app = necro.App.__new__(necro.App)
    app.think, app.show_tools, app.show_edits = False, True, True
    out = app.render(necro.Block("tool", "bash\nls -la"), 80)
    assert any("🔧" in l for l in out)
    monkeypatch.setitem(necro.TOOL_EMOJI, "bash", "🦴")  # restore default


def test_tool_emoji_defaults_ssot():
    """Tool-Emojis sind einmal definiert (palette.EMOJI); cfg 'tool_emoji' ist
    optional (nur fuer Overrides) – necro.cfg ohne den Block, Defaults intakt."""
    import necro
    from modules.core.palette import EMOJI
    for tool in ("read_file", "thinking", "peer"):
        assert necro.TOOL_EMOJI[tool] == EMOJI[tool]
    assert "tool_emoji" not in necro.CFG


def test_peer_turn_timeout_ssot(monkeypatch):
    """peer_turn_timeout: env wins over cfg, both win over the 600 s default."""
    import necro
    monkeypatch.delenv("NECRO_PEER_TURN_TIMEOUT_S", raising=False)
    monkeypatch.setitem(necro.CFG, "peer_turn_timeout_s", 1234)
    assert necro._int("NECRO_PEER_TURN_TIMEOUT_S", "peer_turn_timeout_s", 600) == 1234
    monkeypatch.setenv("NECRO_PEER_TURN_TIMEOUT_S", "777")
    assert necro._int("NECRO_PEER_TURN_TIMEOUT_S", "peer_turn_timeout_s", 600) == 777
    monkeypatch.delenv("NECRO_PEER_TURN_TIMEOUT_S")
    monkeypatch.delitem(necro.CFG, "peer_turn_timeout_s")
    assert necro._int("NECRO_PEER_TURN_TIMEOUT_S", "peer_turn_timeout_s", 600) == 600


def test_thinking_line_emoji_ssot(monkeypatch):
    """the thinking-line prefix uses TOOL_EMOJI['thinking'] (cfg-overridable),
    not a hardcoded emoji."""
    import necro
    app = necro.App.__new__(necro.App)
    app.think, app.show_tools, app.show_edits = False, True, True
    out = app.render(necro.Block("think", "some thought"), 80)
    assert any(necro.TOOL_EMOJI["thinking"] in l for l in out)
    monkeypatch.setitem(necro.TOOL_EMOJI, "thinking", "🔮")
    out = app.render(necro.Block("think", "some thought"), 80)
    assert any("🔮" in l for l in out)


def test_toggle_hints():
    """the think head stays visible after expanding (same line, no hint);
    a tool head with a hidden body gets a toggle hint (Ctrl-o / Ctrl-x)."""
    import necro
    app = necro.App.__new__(necro.App)
    app.think, app.show_tools, app.show_edits = False, True, True
    out = app.render(necro.Block("think", "some thought"), 80)
    assert any(necro.TOOL_EMOJI["thinking"] in l and "thinking..." in l for l in out)
    app.think = True
    out = app.render(necro.Block("think", "some thought"), 80)
    assert any(necro.TOOL_EMOJI["thinking"] in l and "thinking..." in l for l in out)
    assert not any("to show" in l for l in out)
    app.think = False
    # hidden tool body -> hint on the head (distinct text lengths: render cache keys on len)
    out = app.render(necro.Block("tool", "read_file /x\nbody"), 80)
    assert not any("to show" in l for l in out)
    import re
    strip = lambda b: re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(app.render(b, 80)))
    app.show_tools = False
    out = strip(necro.Block("tool", "read_file /x\nbody2"))
    assert "Ctrl-o to show" in out
    assert "read_file /x" in out  # head itself stays visible
    app.show_tools = True
    out = strip(necro.Block("tool", "edit_file f.py\n+ a"))
    assert "to show" not in out
    app.show_edits = False
    out = strip(necro.Block("tool", "edit_file f.py\n+ ab"))
    assert "Ctrl-x to show" in out


def test_emoji_head_width():
    """SMP-Emoji-Tool-Symbole zaehlen 2 Spalten – Heads wrappen korrekt und
    Fortsetzungszeilen rücken einheitlich ein (wie '› bash')."""
    from modules.ui.render import sw
    assert all(sw(e) == 2 for e in ("🕷️", "✒️", "🦴", "💀", "📓"))
    import necro
    app = necro.App.__new__(necro.App)
    app.think, app.show_tools, app.show_edits = False, True, True
    out = app.render(necro.Block("tool", "consult " + "x" * 80), 40)
    assert out[0].startswith("🕷")
    assert out[1].startswith("  ")  # continuation indented by the full emoji width


def test_tool_ssot_constants():
    """the bash timeout + tool output cap are configured once (io.tools) and reused
    (run_bash default, grep cap, agent timeout notes, tool description)."""
    import inspect
    from modules.io import tools
    assert inspect.signature(tools.run_bash).parameters["timeout"].default == tools.BASH_TIMEOUT
    assert tools.cap_text("short") == "short"
    assert tools.cap_text("x" * (tools.TOOL_OUT_CAP + 1)).endswith("[truncated]")


def test_content_cap_ssot(tmp_path):
    """the 20k content cap is configured once (io.tools.CONTENT_CAP) and reused
    (read_file, tool-result truncation in agent, MD context load in necro)."""
    import necro
    from modules.io import tools
    assert necro.CONTENT_CAP is tools.CONTENT_CAP  # necro reuses the same constant
    p = tmp_path / "big.txt"
    p.write_text("a" * (tools.CONTENT_CAP + 5000))
    t = tools.read_file(str(p))
    assert len(t) < tools.CONTENT_CAP + 100  # cut at the cap (+ marker), not the full file
    assert "truncated" in t


def test_edit_diff_flush_left():
    """the edit_file diff is bündig - marker in col 0, number in col 2, content
    in col 8; the rendered body is flush left (no 2-space indent), other tools keep it."""
    import necro
    from modules.ui.render import diff_view
    out = diff_view("l1\nl2\nl3", "l2", "x")
    for ln in out.split("\n")[1:]:  # skip the '...' marker line
        assert ln[7] == " " and ln[2:7].strip().isdigit()
    app = necro.App.__new__(necro.App)
    app.think, app.show_tools, app.show_edits = False, True, True
    lines = app.render(necro.Block("tool", "edit_file f.py\n" + out), 80)
    assert not lines[1].startswith(" ")  # first diff line at column 0
    assert app.render(necro.Block("tool", "bash\nhello"), 80)[1].startswith("  ")


def test_diff_view_sequential_plus_numbers():
    """+ lines (insert/replace) are numbered sequentially, not all the same."""
    from modules.ui.render import diff_view
    def _plus(out):
        return [int(ln.split("     ")[0].split("+ ")[1]) for ln in out.split("\n") if ln.startswith("+")]
    # replace: 3 new lines must get 2,3,4 (not 2,2,2)
    assert _plus(diff_view("l1\nl2\nl3\nl4\nl5\n", "l2\nl3", "X\nY\nZ")) == [2, 3, 4]
    # a larger replace: new lines stay sequential (2..6), not all the same
    assert _plus(diff_view("l1\nl2\nl3\n", "l1", "l1\nNEW1\nNEW2\nl2\nl3\n")) == [2, 3, 4, 5, 6]


def test_recall_head_shows_query():
    """the recall head is not a bare 'recall…' - the query is visible."""
    import necro
    app = necro.App.__new__(necro.App)
    app.think, app.show_tools, app.show_edits = False, True, True
    out = app.render(necro.Block("tool", "recall  deadlock fix"), 80)
    from modules.ui.render import clean
    head = clean(out[0])
    assert "deadlock fix" in head  # the query is shown, not discarded
    assert "recall" in head


# ---------------------------------------------------------------------------
# 3. context.trim_tool_results
# ---------------------------------------------------------------------------
def test_trim_tool_results_huge():
    """A tool message with > 50 000 chars is truncated."""
    from modules.core.context import trim_tool_results
    big = "x" * 60000
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": big},
    ]
    trimmed, n = trim_tool_results(msgs)
    assert n == 1
    assert len(trimmed[1]["content"]) < len(big)
    assert "[trimmed" in trimmed[1]["content"]
    assert str(len(big)) in trimmed[1]["content"]


def test_trim_tool_results_empty():
    """Empty list stays empty."""
    from modules.core.context import trim_tool_results
    trimmed, n = trim_tool_results([])
    assert trimmed == []
    assert n == 0


# ---------------------------------------------------------------------------
# 4. render.sgr / wrap_segs round-trip
# ---------------------------------------------------------------------------
def test_sgr_produces_codes():
    """sgr() returns a string containing ANSI escape sequences."""
    from modules.ui.render import sgr
    code = sgr("38;5;16", "b")
    assert "\x1b[" in code
    assert "m" in code


def test_wrap_segs_width():
    """wrap_segs breaks lines so no visual line exceeds `width`."""
    from modules.ui.render import wrap_segs
    segs = [("A" * 200, "")]
    lines = wrap_segs(segs, 40)
    for line in lines:
        from modules.ui.render import sw
        assert sw("".join(t for t, _ in line)) <= 40


# ---------------------------------------------------------------------------
# 5. Agent loop with mock HTTP server
# ---------------------------------------------------------------------------
class _MockHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible mock."""

    def log_message(self, *a):
        pass  # silence stderr

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        stream = body.get("stream", False)

        if stream:
            # SSE stream: one delta chunk + one usage chunk
            sse = (
                'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                'data: {"usage":{"prompt_tokens":42,"completion_tokens":5}}\n\n'
                'data: [DONE]\n\n'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(sse.encode())))
            self.end_headers()
            self.wfile.write(sse.encode())
        else:
            # Non-streaming JSON response
            payload = json.dumps({
                "choices": [{"message": {"content": "ok"}}]
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload.encode())))
            self.end_headers()
            self.wfile.write(payload.encode())


def test_agent_stream_and_complete():
    """Start a ThreadingHTTPServer on ephemeral port, exercise agent.stream()
    and agent.complete() against it."""
    # -- start mock server on ephemeral port --------------------------------
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    host, port = srv.server_address
    base_url = f"http://{host}:{port}"
    srv_thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    srv_thread.start()

    try:
        # -- set up a temp DB so we don't pollute data/necro.db ---------------
        tmp_db = tempfile.mktemp(suffix=".db")
        os.environ["NECRO_DB"] = tmp_db

        # -- import necro (no side-effects beyond cfg read) -------------------
        import necro
        app = necro.App()

        # -- override base URL and model -------------------------------------
        app.agent.base = base_url
        app.model = "mock"

        # -- inject a user message -------------------------------------------
        app.messages.append({"role": "user", "content": "smoke test"})

        # -- stream ----------------------------------------------------------
        text, calls = app.agent.stream()
        assert text == "hello", f"expected 'hello', got {text!r}"
        assert app.ctx_used == 42, f"expected ctx_used=42, got {app.ctx_used}"

        # -- complete (non-streaming) ----------------------------------------
        result = app.agent.complete(app.messages)
        assert result == "ok", f"expected 'ok', got {result!r}"

    finally:
        srv.shutdown()
        # clean up temp db
        try:
            os.unlink(tmp_db)
        except OSError:
            pass
