"""edge robustness - a reply without content/message must give a precise
error string, never a TypeError/AttributeError traceback; and modules must import
without a necro.cfg in the cwd (no config side effects at import time)."""
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from modules.core import agent as agent_mod
from modules.core.agent import Agent, ReplyError, content_of, parse_reply

BODIES = {
    "ok": {"choices": [{"message": {"content": "hallo"}}]},
    "no_content": {"choices": [{"message": {}}]},
    "null_content": {"choices": [{"message": {"content": None}}]},
    "null_message": {"choices": [{"message": None}]},
    "no_choices": {"choices": []},
    "null_choices": {"choices": None},
    "not_a_dict": [{"choices": [{"message": {"content": "x"}}]}],
    "parts": {"choices": [{"message": {"content": [{"type": "text", "text": "a"},
                                                 {"type": "image", "data": "..."},
                                                 {"type": "text", "text": "b"}]}}]},
}


# ---------------------------------------------------------------- pure helpers
@pytest.mark.parametrize("name", ["no_content", "null_content"])
def test_content_of_missing_content_is_empty(name):
    assert content_of(parse_reply(json.dumps(BODIES[name]))) == ""


def test_content_of_parts_and_plain():
    assert content_of({"content": "text"}) == "text"
    assert content_of(parse_reply(json.dumps(BODIES["parts"]))) == "ab"  # text parts only
    assert content_of(None) == "" and content_of({"content": 42}) == ""  # guard: wrong type


@pytest.mark.parametrize("name,reason", [
    ("null_message", "message"),
    ("no_choices", "choices"),
    ("null_choices", "choices"),
    ("not_a_dict", "choices"),
])
def test_parse_reply_malformed_raises_reply_error(name, reason):
    with pytest.raises(ReplyError) as ei:
        parse_reply(json.dumps(BODIES[name]))
    assert reason in str(ei.value)
    assert not isinstance(ei.value, (TypeError, AttributeError, IndexError, KeyError))


def test_parse_reply_non_json():
    with pytest.raises(ReplyError) as ei:
        parse_reply(b"<html>502 Bad Gateway</html>")
    assert "not JSON" in str(ei.value)


# ------------------------------------------------------- end to end: consult
@pytest.fixture
def reply_server():
    """Local OpenAI-style server whose reply body is switched per test."""
    state = {"name": "ok"}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            b = json.dumps(BODIES[state["name"]]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{srv.server_port}/v1"
    yield state
    srv.shutdown()


class _App:
    """Minimal App stand-in: the shared state the Agent touches (no terminal)."""
    model, reasoning, tag = "test-model", False, None

    def __init__(self):
        self.messages = [{"role": "system", "content": "sys"}]
        self.stop, self.conns = threading.Event(), []
        self.blocks = []

    def add(self, kind, text="", color=None):
        self.blocks.append(text)
        return type("B", (), {"kind": kind, "text": text})()

    def append(self, blk, text):
        blk.text += text

    def set_head(self, blk, head):
        blk.text = head


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """No backoff sleeps in the tests: one attempt is enough to see the error."""
    monkeypatch.setattr(agent_mod, "RETRY_ATTEMPTS", 1)


def _agent(base):
    """Agent with a one-model swarm so the consult tool can route to the fake server."""
    swarm = [{"url": base, "model": "m"}]
    return Agent(_App(), base, 5, [], [], swarm, {}, "", "")


def test_consult_missing_message_is_error_pointer_not_traceback(reply_server):
    """The observed crash: a reply body without message -> precise error, no TypeError."""
    reply_server["name"] = "null_message"
    ag, app = _agent(reply_server["base"]), None
    with pytest.raises(ReplyError) as ei:
        ag.consult({"url": reply_server["base"], "model": "m"}, "frage")
    assert "message" in str(ei.value)
    # through run_tool it becomes a defined error string (the model sees it, no traceback)
    out = ag.run_tool({"name": "consult", "args": json.dumps({"model": "m", "question": "q"})})
    assert out.startswith("error: ReplyError: ") and "message" in out
    assert "TypeError" not in out


def test_consult_empty_reply_is_reported(reply_server):
    reply_server["name"] = "no_content"
    ag = _agent(reply_server["base"])
    with pytest.raises(ReplyError) as ei:
        ag.consult({"url": reply_server["base"], "model": "m"}, "frage")
    assert "empty reply" in str(ei.value)


def test_consult_ok_still_works(reply_server):
    reply_server["name"] = "ok"
    ag = _agent(reply_server["base"])
    assert ag.consult({"url": reply_server["base"], "model": "m"}, "frage") == "hallo"


def test_peer_prompt_malformed_reply(monkeypatch):
    """A peer answering with a body without 'result' must not raise KeyError."""
    import io
    from modules.io import api

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp(b'{"ok": true}'))
    out = api.peer_prompt({"id": "abc", "port": 1, "token": "t"}, "task", timeout=1)
    assert out.startswith("error: peer abc sent a malformed reply")


# ------------------------------------------------- no import-time config side effect
def test_modules_import_without_necro_cfg(tmp_path):
    """modules/ must not read necro.cfg at import time (no ci
    side effects): a plain import in an empty cwd works."""
    root = Path(__file__).resolve().parent.parent
    for mod in ("modules.core.agent", "modules.core.turn", "modules.ui.view", "modules.io.api"):
        r = subprocess.run([sys.executable, "-c", f"import {mod}"], cwd=tmp_path,
                           env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{mod}: {r.stderr.strip()}"
        assert not (tmp_path / "necro.cfg").exists()  # nothing was created either


def test_no_module_imports_necro_toplevel():
    """The layer rule: modules/ never imports necro (that would re-run the config
    loader at import time). necro.py injects its values instead."""
    root = Path(__file__).resolve().parent.parent / "modules"
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if "import necro" in p.read_text(errors="replace")]
    assert offenders == []


def test_config_module_has_no_cfg_global():
    from modules.core import config
    assert not hasattr(config, "CFG")  # validate(cfg) takes the dict as a parameter
