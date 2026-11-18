"""N251: the model lookup must support both routes a provider may offer.

OpenAI answers a single model on GET /v1/models/<id>; OpenRouter answers on
GET /api/v1/model/<id> (singular) and publishes the window as `context_length`.
The list route (/v1/models) stays the fallback. No extra provider row: the
openai default covers both.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modules.core import probe

MODEL = "nvidia/nemotron-3.5-lightning:free"
SINGLE = {"id": MODEL, "name": "NVIDIA: Nemotron 3.5 Lightning (free)", "created": 1786452751,
          "context_length": 1000000,
          "architecture": {"modality": "text->text", "input_modalities": ["text"],
                           "output_modalities": ["text"]},
          "default_parameters": {"temperature": 0.7, "top_p": 0.95}}


class _Server(BaseHTTPRequestHandler):
    """Routes: single_lookup (path -> object or 404), list (list answer or 404)."""
    routes = {}
    seen = []

    def do_GET(self):  # noqa: N802 (http.server API)
        type(self).seen.append(self.path)
        route = type(self).routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(route).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep the test output clean
        pass


@pytest.fixture
def server():
    def make(routes):
        _Server.routes, _Server.seen = routes, []
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}/api/v1"
    made = []
    def factory(routes):
        srv, base = make(routes)
        made.append(srv)
        return base
    yield factory
    for srv in made:
        srv.shutdown()
        srv.server_close()


def test_openrouter_single_route(server):
    """The wrapper 'data' and the flat `context_length` must be understood."""
    base = server({f"/api/v1/model/{MODEL}": {"data": SINGLE}})
    info = probe.fetch_model_info(base, MODEL, 8192)
    assert info["ctx"] == 1000000 and info["arch"] == "text->text"
    assert info["status"] == "NVIDIA: Nemotron 3.5 Lightning (free)"
    assert info["temp"] == "0.7" and info["top_p"] == "0.95"
    assert f"/api/v1/model/{MODEL}" in _Server.seen  # the singular route was asked


def test_openai_single_route(server):
    """OpenAI returns the model object bare on /models/<id>."""
    base = server({f"/api/v1/models/{MODEL}": {**SINGLE, "context_length": 128000}})
    info = probe.fetch_model_info(base, MODEL, 8192)
    assert info["ctx"] == 128000


def test_list_route_is_the_fallback(server):
    """No single route: the list answer must still fill ctx (context_length)."""
    base = server({"/api/v1/models": {"data": [SINGLE]}})
    info = probe.fetch_model_info(base, MODEL, 8192)
    assert info["ctx"] == 1000000 and info["arch"] == "text->text"


def test_other_model_is_not_accepted(server):
    """A single answer for a different id must not be reported as ours."""
    base = server({f"/api/v1/model/{MODEL}": {"data": {**SINGLE, "id": "other"}}})
    info = probe.fetch_model_info(base, MODEL, 8192)
    assert info["ctx"] == 8192 and info["status"] == "hosted"  # plain hosted fallback


def test_broken_json_falls_back(server):
    """A proxy landing page (HTTP 200, no JSON) must not raise."""
    class _Html(_Server):
        def do_GET(self):  # noqa: N802
            body = b"<html>hello</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    _Server.routes = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Html)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        info = probe.fetch_model_info(f"http://127.0.0.1:{srv.server_address[1]}/v1", MODEL, 8192)
    finally:
        srv.shutdown()
        srv.server_close()
    assert info["ctx"] == 8192 and info["status"] == "hosted"


def test_colon_and_slash_stay_in_the_path():
    """The model id is a path, not a query value: ':' and '/' must survive."""
    from urllib.parse import quote
    assert quote(MODEL, safe="/:@") == MODEL
