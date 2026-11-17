"""Model info: the reasoning state comes from the SERVER, not from the request
toggle. A llama.cpp preset started with `reasoning = false` /
`--reasoning-budget 0` emits no reasoning_content even when the request asks for
thinking - NECRO showed `think:on` in the header and `Reasoning effort: -` in
/model at the same time (Qwen3.8-27B-Swift).
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modules.core import probe
from modules.ui import view as view_mod

# real /models args of the server presets (reduced to the relevant flags)
SWIFT = ["llama-server", "--alias", "Qwen3.8-27B-Swift", "--ctx-size", "131072",
         "--reasoning-budget", "0", "--reasoning", "false", "--threads", "8"]
TEMPLATE_LOW = ["--chat-template-kwargs", '{"reasoning_effort":"low"}']
ORNITH = ["llama-server", "--reasoning-preserve", "--ctx-size", "131072"]


@pytest.mark.parametrize("args, want", [
    (SWIFT, "off"),                       # --reasoning false
    (["--reasoning-budget", "0"], "off"),  # no tokens left for thinking
    (["--reasoning", "true"], "on"),
    (["--reasoning-budget", "-1"], "on"),  # unlimited budget
    (["--reasoning-budget", "512"], "on"),
    (TEMPLATE_LOW, "low"),                # template effort level
    (["--reasoning-effort", "high"], "high"),
    (ORNITH, ""),                         # --reasoning-preserve is not the switch
    (["--reasoning-budget", "abc"], ""),  # malformed: no claim in either direction
    (["--reasoning"], ""),                # dangling flag
    ([], ""),
])
def test_reasoning_state_from_server_args(args, want):
    assert probe._reasoning(args) == want


def test_effort_level_wins_over_the_switch():
    """The template effort says more than the on/off switch."""
    assert probe._reasoning(["--reasoning", "false"] + TEMPLATE_LOW) == "low"


def test_dangling_flag_yields_no_value():
    assert probe._arg(["--reasoning"], "--reasoning") == ""
    assert probe._arg([], "--reasoning") == ""


def test_info_text_shows_the_server_state():
    info = probe.default_info(131072)
    info.update({"status": "loaded", "effort": probe._reasoning(SWIFT)})
    assert "Reasoning effort: off" in probe.info_text("Qwen3.8-27B-Swift", info)


class _Models(BaseHTTPRequestHandler):
    """Minimal /v1/models answer (llama.cpp model_manager shape)."""
    def do_GET(self):  # noqa: N802 (http.server API)
        body = json.dumps({"data": [{"id": "Qwen3.8-27B-Swift", "created": 1794924338,
                                     "architecture": {"input_modalities": ["text"],
                                                      "output_modalities": ["text"]},
                                     "status": {"value": "loaded", "args": SWIFT}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep the test output clean
        pass


@pytest.fixture
def mock_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Models)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


def test_fetch_model_info_reads_the_reasoning_state(mock_server):
    info = probe.fetch_model_info(mock_server, "Qwen3.8-27B-Swift", 72000)
    assert info["effort"] == "off" and info["status"] == "loaded"
    assert info["ctx"] == 131072  # the server window, the cap is applied by the App


@pytest.fixture
def necro_mod(tmp_path, monkeypatch):
    """NECRO with the background probes switched off (no network in unit tests)."""
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    monkeypatch.setenv("NECRO_PEER_DIR", str(tmp_path / "peers"))
    import necro
    monkeypatch.setattr(necro, "_resolve_model_info", lambda app: None)
    return necro


def _draw(necro, app, monkeypatch, w, h):
    monkeypatch.setattr(view_mod.shutil, "get_terminal_size", lambda *a: os.terminal_size((w, h)))
    out = []
    monkeypatch.setattr(necro.os, "write", lambda fd, data: out.append(data))
    app.draw()
    return b"".join(out).decode()


def test_header_shows_the_server_truth_next_to_the_toggle(necro_mod, monkeypatch):
    """Request asks for thinking, the server answers 'off': both must be visible."""
    app = necro_mod.App()
    app.buf, app.cur = "", 0
    app.reasoning, app.reasoning_effort = True, "off"
    assert "think:on/off" in _draw(necro_mod, app, monkeypatch, 100, 30)
    app.reasoning = False  # toggle also off: no conflict to report
    assert "think:off ·" in _draw(necro_mod, app, monkeypatch, 100, 30)
    app.reasoning, app.reasoning_effort = True, ""  # server says nothing: the toggle shows
    assert "think:on ·" in _draw(necro_mod, app, monkeypatch, 100, 30)


def test_model_info_names_the_ctx_cap(necro_mod, monkeypatch):
    """72000 next to a server --ctx-size of 131072 looks wrong without the cap note."""
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": 72000})
    app = necro_mod.App()
    app.model, app.model_info = "Qwen3.8-27B-Swift", probe.default_info(131072)
    assert app.set_ctx(131072) == 72000
    app.model_info["ctx"] = app.ctx
    assert "72000 (cap ctx_models['Qwen3-8'])" in app.model_info_text()
    app.ctx_capped_key = None
    assert "72000 (cap" not in app.model_info_text()
