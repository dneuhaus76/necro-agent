"""optional per-server access tokens (cfg 'auth')."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from modules.core import inference


@pytest.fixture(autouse=True)
def _isolate_api_token():
    """The container sets NECRO_API_TOKEN, which token_for() checks FIRST and
    would override the cfg 'auth' token under test. Delete it so each test is
    isolated from the ambient env."""
    import os
    monkeypatch_del = os.environ.pop("NECRO_API_TOKEN", None)
    yield
    if monkeypatch_del is not None:
        os.environ["NECRO_API_TOKEN"] = monkeypatch_del


def test_token_for_exact_url():
    assert inference.token_for("http://192.168.1.149:8080/v1",
                              {"http://192.168.1.149:8080/v1": "sekret"}) == "sekret"


def test_token_for_host_port():
    assert inference.token_for("http://192.168.1.149:8080/v1",
                              {"192.168.1.149:8080": "sekret"}) == "sekret"


def test_token_for_hostname_only():
    assert inference.token_for("http://192.168.1.149:8080/v1",
                              {"192.168.1.149": "sekret"}) == "sekret"


def test_token_for_absent_is_none():
    assert inference.token_for("http://192.168.1.149:8080/v1", {"other:8080": "x"}) is None
    assert inference.token_for("http://192.168.1.149:8080/v1", None) is None
    assert inference.token_for("http://192.168.1.149:8080/v1", "not-a-dict") is None


def test_open_conn_sends_bearer_header():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            body = b'{"choices":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/v1"
    try:
        conn, r = inference.open_conn(base, {"model": "m"}, timeout=5, token="sekret")
        r.read()
        conn.close()
        assert seen["auth"] == "Bearer sekret"
        # without a token: no Authorization header at all (optional, N174)
        conn, r = inference.open_conn(base, {"model": "m"}, timeout=5)
        r.read()
        conn.close()
        assert seen["auth"] is None
    finally:
        srv.shutdown()


def test_open_conn_has_no_default_timeout():
    """cfg 'step_timeout_s' is the only owner of the value: a default here would be
    a second copy that drifts (the 900 s relic was exactly that). A caller that
    forgets the timeout must fail loudly, not fall back to a number."""
    import inspect
    assert inspect.signature(inference.open_conn).parameters["timeout"].default is None
    with pytest.raises(ValueError):
        inference.open_conn("http://127.0.0.1:9/v1", {"model": "m"})


def test_validate_auth_key():
    from modules.core.config import validate
    assert validate({"auth": {"http://x:8080/v1": "tok"}}) == []
    assert any("auth" in i for i in validate({"auth": "not-a-dict"}))
