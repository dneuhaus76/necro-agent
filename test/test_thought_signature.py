"""Google thought signatures must survive the tool-call round trip.

Google's OpenAI-compatible endpoint returns the signature as
extra_content.google.thought_signature on every function call and rejects a
replayed call without it (HTTP 400 'Function call is missing a thought_signature').
NECRO rebuilds assistant messages from id/name/args, so the payload was dropped.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modules.core import inference

SIG = "SIG-abc123+/="


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


# ---- pure helpers

def test_call_extra_from_extra_content():
    tc = {"id": "c1", "extra_content": {"google": {"thought_signature": SIG}}}
    assert inference.call_extra(tc) == {"extra_content": {"google": {"thought_signature": SIG}}}


def test_call_extra_from_bare_signature():
    for tc in ({"thought_signature": SIG}, {"function": {"name": "grep", "thought_signature": SIG}}):
        assert inference.call_extra(tc) == {"extra_content": {"google": {"thought_signature": SIG}}}


def test_call_extra_absent_is_empty():
    assert inference.call_extra({"id": "c1", "function": {"name": "grep", "arguments": "{}"}}) == {}
    assert inference.call_extra({"extra_content": "nonsense"}) == {}


def test_call_extra_is_detached_copy():
    tc = {"extra_content": {"google": {"thought_signature": SIG}}}
    ex = inference.call_extra(tc)
    ex["extra_content"]["google"]["thought_signature"] = "changed"
    assert tc["extra_content"]["google"]["thought_signature"] == SIG


def test_merge_extra_one_level():
    dst = {"extra_content": {"google": {"thought_signature": SIG}}}
    inference.merge_extra(dst, {"extra_content": {"google": {}}, "other": 1})
    assert dst == {"extra_content": {"google": {"thought_signature": SIG}}, "other": 1}


def test_assistant_msg_replays_extra():
    calls = [{"id": "c1", "name": "grep", "args": '{"pattern": "x"}',
              "extra": inference.call_extra({"extra_content": {"google": {"thought_signature": SIG}}})}]
    m = inference.assistant_msg("", calls)
    assert m["role"] == "assistant" and m["content"] is None
    tc = m["tool_calls"][0]
    assert tc["id"] == "c1" and tc["type"] == "function"
    assert tc["function"] == {"name": "grep", "arguments": '{"pattern": "x"}'}
    assert tc["extra_content"]["google"]["thought_signature"] == SIG


def test_assistant_msg_plain_without_extra():
    m = inference.assistant_msg("text", [{"id": "c1", "name": "grep", "args": ""}])
    assert m["tool_calls"] == [{"id": "c1", "type": "function",
                                "function": {"name": "grep", "arguments": "{}"}}]
    assert inference.assistant_msg("done", []) == {"role": "assistant", "content": "done"}


# ---- round trip over a Google-like SSE stream

def _chunk(tc=None, content=None):
    d = {}
    if content is not None:
        d["content"] = content
    if tc is not None:
        d["tool_calls"] = [tc]
    return {"choices": [{"index": 0, "delta": d}]}


class GoogleHandler(BaseHTTPRequestHandler):
    """streams one tool call (with thought signature) on the first request,
    plain text on the second; records every request body."""
    bodies = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).bodies.append(body)
        first = len(type(self).bodies) == 1
        chunks = ([_chunk({"index": 0, "id": "call_1",
                           "function": {"name": "grep", "arguments": '{"pattern": "x"}'},
                           "extra_content": {"google": {"thought_signature": SIG}}}),
                   _chunk({}, content="done")]
                  if first else
                  [_chunk(content="second")]
                  + [_chunk({"index": 0, "id": "call_2", "function": {"name": "grep"},
                             "extra_content": {"google": {"thought_signature": "SIG2"}}})])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


def test_stream_keeps_signature_and_replays_it(env):
    GoogleHandler.bodies = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), GoogleHandler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        app = env.App()
        app.model = "gemini-test"
        app.agent.base = f"http://127.0.0.1:{srv.server_address[1]}"
        app.reasoning = False
        app.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        text, calls = app.agent.stream()
        assert text == "done" and calls[0]["name"] == "grep"
        assert calls[0]["extra"]["extra_content"]["google"]["thought_signature"] == SIG
        app.messages.append(inference.assistant_msg(text, calls))
        app.messages.append({"role": "tool", "tool_call_id": calls[0]["id"], "content": "hit"})
        # second step: the replayed call must carry the signature again
        app.agent.stream()
        sent = GoogleHandler.bodies[1]["messages"][2]
        assert sent["role"] == "assistant"
        assert sent["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == SIG
        assert GoogleHandler.bodies[1]["messages"][3] == {
            "role": "tool", "tool_call_id": "call_1", "content": "hit"}
    finally:
        srv.shutdown()


def test_complete_tools_keeps_signature(env):
    """swarm sub-agents go through complete_tools (non-streaming) - same payload."""
    body = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "grep", "arguments": "{}"},
         "extra_content": {"google": {"thought_signature": SIG}}}]}}]}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            b = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        app = env.App()
        app.reasoning = False
        _, calls = app.agent.complete_tools([{"role": "user", "content": "u"}],
                                           f"http://127.0.0.1:{srv.server_address[1]}", "m")
        assert calls[0]["extra"]["extra_content"]["google"]["thought_signature"] == SIG
    finally:
        srv.shutdown()
