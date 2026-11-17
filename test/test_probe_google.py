"""Google's OpenAI-compatible endpoint has no /models - the model details come
from the native API (/v1beta/models/<id>). The path must keep the version prefix
(without it Google answers 404 and /model looks empty) and the key must not
travel in the URL."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.core import probe  # noqa: E402

BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
NATIVE = {"name": "models/gemini-3.5-flash-lite", "version": "3.5-flash-lite-07-2026",
          "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
          "temperature": 1, "topP": 0.95, "topK": 64, "thinking": True}


class FakeResp:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    def read(self):
        return json.dumps(self.payload).encode()


class FakeConn:
    """Records the requests; replays the given (status, payload) responses."""
    def __init__(self, responses):
        self.responses, self.calls, self.closed = responses, [], False  # shared queue, consumed in order

    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path, dict(headers or {})))

    def getresponse(self):
        return FakeResp(*self.responses.pop(0))

    def close(self):
        self.closed = True


def _patch(monkeypatch, *responses):
    """One connection per attempt, each replaying the next queued response."""
    queue, conns = list(responses), []

    def fake_conn(base, timeout, token=None):
        conns.append(FakeConn(queue))
        return conns[-1], None, token
    monkeypatch.setattr(probe, "_conn", fake_conn)
    return conns


def test_native_path_keeps_version_prefix(monkeypatch):
    conns = _patch(monkeypatch, (200, NATIVE))
    info = probe._google_model_info("https://x/", "gemini-3.5-flash-lite", 8192, "KEY")
    assert conns[0].calls[0][1] == "/v1beta/models/gemini-3.5-flash-lite"
    assert info["ctx"] == 1048576
    assert info["status"] == "hosted (3.5-flash-lite-07-2026)"
    assert info["effort"] == "on" and info["temp"] == "1" and info["top_p"] == "0.95" and info["top_k"] == "64"
    assert conns[0].closed is True


def test_model_id_prefixes_are_stripped(monkeypatch):
    for name in ("models/gemini-3.5-flash-lite", "google/gemini-3.5-flash-lite"):
        conns = _patch(monkeypatch, (200, NATIVE))
        probe._google_model_info("https://x/", name, 8192, "KEY")
        assert conns[0].calls[0][1] == "/v1beta/models/gemini-3.5-flash-lite"


def test_key_travels_in_header_not_in_url(monkeypatch):
    conns = _patch(monkeypatch, (200, NATIVE))
    probe._google_model_info("https://x/", "gemini-3.5-flash-lite", 8192, "SECRET")
    method, path, headers = conns[0].calls[0]
    assert "SECRET" not in path and "key=" not in path
    assert headers["x-goog-api-key"] == "SECRET"


def test_auth_error_falls_back_to_bearer(monkeypatch):
    conns = _patch(monkeypatch, (401, {"error": "no key"}), (200, NATIVE))
    info = probe._google_model_info("https://x/", "gemini-3.5-flash-lite", 8192, "OAUTH")
    assert len(conns) == 2
    assert conns[1].calls[0][2]["Authorization"] == "Bearer OAUTH"
    assert info["ctx"] == 1048576


def test_unknown_model_reports_the_failed_lookup(monkeypatch):
    _patch(monkeypatch, (404, {"error": "not found"}))
    assert probe._google_model_info("https://x/", "nope", 8192, "KEY") is None
    info = probe.fetch_model_info(BASE, "nope", 8192, "KEY")
    assert info["status"] == "hosted - native API lookup failed"
    assert info["ctx"] == 8192  # configured fallback kept


def test_fetch_model_info_uses_the_native_lookup(monkeypatch):
    _patch(monkeypatch, (200, NATIVE))
    info = probe.fetch_model_info(BASE, "gemini-3.5-flash-lite", 8192, "KEY")
    assert info["ctx"] == 1048576
    text = probe.info_text("gemini-3.5-flash-lite", info)
    for field, value in (("Status", "hosted"), ("Reasoning effort", "on"), ("ctx-size", "1048576"),
                         ("architecture", "text->text"), ("temperature", "1")):
        assert f"{field:<16}: {value}" in text  # the interesting rows are filled, not "-"


def test_missing_token_sends_no_credentials(monkeypatch):
    """No token configured: the request must carry no auth header (and the answer,
    if the host allows anonymous reads, is still used)."""
    conns = _patch(monkeypatch, (200, NATIVE))
    info = probe.fetch_model_info(BASE, "gemini-3.5-flash-lite", 8192, None)
    headers = conns[0].calls[0][2]
    assert "Authorization" not in headers and "x-goog-api-key" not in headers
    assert info["ctx"] == 1048576


def test_auth_failure_ends_in_the_labelled_fallback(monkeypatch):
    conns = _patch(monkeypatch, (401, {"error": "bad key"}), (403, {"error": "bad key"}))
    info = probe.fetch_model_info(BASE, "gemini-3.5-flash-lite", 8192, "BAD")
    assert len(conns) == 2  # key header, then Bearer
    assert info["status"] == "hosted - native API lookup failed"
