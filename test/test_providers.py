"""Provider profiles: one table decides what a provider needs differently
(thinking field, model-info API, key header). A runner whose hostname gives no
hint (local IP, proxy) is named explicitly: cfg 'provider' for the main
endpoint, 'provider' in a cfg 'swarm' entry for a swarm server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.core import inference, probe, providers  # noqa: E402

GOOGLE_OPENAI = "https://generativelanguage.googleapis.com/v1beta/openai/"


def test_plain_server_is_openai():
    p = providers.resolve("http://127.0.0.1:8080/v1")
    assert p is providers.OPENAI
    assert p.thinking == "bool" and p.info_api == "" and p.key_header == ""


def test_google_and_deepseek_by_hostname():
    assert providers.resolve(GOOGLE_OPENAI) is providers.GOOGLE
    assert providers.resolve("https://api.deepseek.com/v1") is providers.DEEPSEEK


def test_suffix_match_needs_a_dot_boundary():
    """'evilgoogleapis.com' must not be treated as Google."""
    assert providers.resolve("https://evilgoogleapis.com/v1") is providers.OPENAI


def test_thinking_field_per_provider():
    assert inference.thinking_value("http://127.0.0.1:8080/v1", True) is True
    assert inference.thinking_value("https://api.deepseek.com/v1", False) == {"type": "disabled"}
    assert inference.thinking_value(GOOGLE_OPENAI, True) is None  # field is omitted


def test_explicit_name_wins_over_the_hostname():
    """Proxy in front of Gemini: the hostname says nothing, the name decides."""
    base = "https://my-proxy.local/v1"
    assert providers.resolve(base, "google") is providers.GOOGLE
    assert inference.thinking_value(base, True, "google") is None


def test_explicit_name_is_case_insensitive():
    assert providers.resolve("http://10.0.0.5:8080/v1", "DeepSeek").name == "deepseek"
    assert providers.name_of(" BigMoe ") == "bigmoe"


def test_invalid_name_falls_back_silently():
    assert providers.resolve("http://127.0.0.1:8080/v1", "typo").name == "openai"
    assert providers.resolve("http://127.0.0.1:8080/v1", None).name == "openai"
    assert providers.resolve("http://127.0.0.1:8080/v1", 3).name == "openai"


def test_native_headers_use_the_key_header_only_for_native_apis():
    h = providers.native_headers(providers.GOOGLE, "SECRET")
    assert h["x-goog-api-key"] == "SECRET" and "Authorization" not in h
    h = providers.native_headers(providers.OPENAI, "SECRET")
    assert h["Authorization"] == "Bearer SECRET"


def test_probe_dispatches_by_profile(monkeypatch):
    """fetch_model_info must reach the native loader for the google profile only."""
    calls = []
    monkeypatch.setitem(probe.NATIVE_INFO, "google", lambda b, m, c, t: calls.append((m, c, t)) or {"native": 1})
    assert probe.fetch_model_info(GOOGLE_OPENAI, "gemini-x", 8192, "K") == {"native": 1}
    assert calls == [("gemini-x", 8192, "K")]
    assert probe.fetch_model_info("http://127.0.0.1:8080/v1", "local", 8192, None)["ctx"] == 8192


def test_probe_name_reaches_the_loader(monkeypatch):
    monkeypatch.setitem(probe.NATIVE_INFO, "google", lambda b, m, c, t: {"via": "name"})
    info = probe.fetch_model_info("https://proxy.local/v1", "gemini-x", 8192, None, "google")
    assert info == {"via": "name"}


def _info():
    """A full model-info dict (what a probe may deliver for any provider)."""
    from modules.core.probe import default_info
    i = default_info(8192)
    i.update({"status": "hosted (v1)", "effort": "on", "ctx": 1048576, "temp": "1", "top_k": "64",
              "top_p": "0.95", "min_p": "0", "arch": "text->text", "file": "", "gpu_layers": "99",
              "threads": "8", "flash_attn": "on", "repeat_penalty": "1.1", "presence_penalty": "0",
              "kv_cache": "q8_0/q8_0", "created": "2026-11-15"})
    return i


def test_model_rows_follow_the_provider():
    """/model shows what the provider can have - not nine rows of "-"."""
    text = probe.info_text("gemini-3.5-flash-lite", _info(), providers.GOOGLE)
    assert "ctx-size        : 1048576" in text
    assert "temperature     : 1" in text and "top-k           : 64" in text and "top-p           : 0.95" in text
    assert "architecture    : text->text" in text
    for absent in ("gpu-layers", "threads", "flash-attn", "kv-cache", "min-p", "created"):
        assert absent not in text


def test_local_server_still_shows_all_rows():
    text = probe.info_text("Qwen3-8-Q8_0.gguf", _info())
    for present in ("gpu-layers", "threads", "flash-attn", "kv-cache", "min-p", "created", "repeat-penalty"):
        assert present in text


def test_deepseek_rows_are_a_minimal_set():
    text = probe.info_text("deepseek-chat", _info(), providers.DEEPSEEK)
    assert "ctx-size" in text and "top-p" in text and "architecture" in text
    assert "top-k" not in text and "gpu-layers" not in text


def test_app_model_command_uses_the_profile(monkeypatch):
    """The App hands its resolved profile to info_text (no second hostname check)."""
    import necro
    a = object.__new__(necro.App)
    a.model, a.model_info = "gemini-3.5-flash-lite", _info()
    monkeypatch.setattr(necro, "BASE_URL", GOOGLE_OPENAI)
    out = a.model_info_text()
    assert "ctx-size" in out and "gpu-layers" not in out


def test_provider_name_from_cfg_and_env(monkeypatch):
    """cfg 'provider' names the runner directly; env NECRO_PROVIDER wins; a typo
    is ignored (it must not break startup)."""
    assert providers.provider_name({"provider": "bigmoe"}) == "bigmoe"
    assert providers.provider_name({"provider": "BigMoe"}) == "bigmoe"
    assert providers.provider_name({"provider": "nope"}) is None
    assert providers.provider_name(None) is None
    monkeypatch.setenv("NECRO_PROVIDER", "deepseek")
    assert providers.provider_name({"provider": "bigmoe"}) == "deepseek"  # env wins


def test_resolve_explicit_name_beats_hostname(monkeypatch):
    monkeypatch.delenv("NECRO_PROVIDER", raising=False)
    # a local IP with no hostname hint: the explicit name decides
    assert providers.resolve("http://192.168.1.149:8080/v1", "bigmoe") is providers.BIGMOE
    # without a name the OpenAI default still applies
    assert providers.resolve("http://192.168.1.149:8080/v1") is providers.OPENAI


def test_bigmoe_loader_reads_api_info(monkeypatch):
    """BigMoeOnEdge: model details come from GET /api/info, not /models."""
    import json as _json
    from modules.core import probe

    body = _json.dumps({"state": "ready", "model": {
        "path": "/opt/models/Ornith.gguf", "name": "Ornith.gguf", "arch": "qwen35moe",
        "n_ctx": 65536, "compute_threads": 16, "load_s": 0.7}}).encode()

    class FakeResp:
        def __init__(self, status, data=b""):
            self.status, self.data = status, data

        def read(self):
            return self.data

    class FakeConn:
        """The real runner answers on the root path only; '/v1/api/info' is 404."""
        def __init__(self):
            self.calls = []

        def request(self, method, path, body=None, headers=None):
            self.calls.append((method, path))

        def getresponse(self):
            if self.calls[-1][1] == "/api/info":
                return FakeResp(200, body)
            return FakeResp(404, b'{"error":"not found"}')

        def close(self):
            pass

    conns = []

    from urllib.parse import urlparse
    def fake_conn(base, timeout, token=None):
        conns.append(FakeConn())
        return conns[-1], urlparse(base), token
    monkeypatch.setattr(probe, "_conn", fake_conn)
    info = probe._bigmoe_model_info("http://192.168.1.149:8080/v1", "Ornith.gguf", 8192, None)
    assert conns[0].calls == [("GET", "/api/info")]  # root path first, no detour over /v1
    assert info["ctx"] == 65536 and info["arch"] == "qwen35moe"
    assert info["file"] == "Ornith.gguf" and info["threads"] == "16"
    assert info["status"] == "ready"


def test_bigmoe_profile_dispatch_in_fetch(monkeypatch):
    """fetch_model_info with name='bigmoe' must reach the bigmoe loader."""
    from modules.core import probe
    monkeypatch.setitem(probe.NATIVE_INFO, "bigmoe", lambda b, m, c, t: {"via": "bigmoe"})
    info = probe.fetch_model_info("http://192.168.1.149:8080/v1", "Ornith.gguf", 8192, None, "bigmoe")
    assert info == {"via": "bigmoe"}


def test_swarm_entry_keeps_its_provider():
    """cfg 'swarm' entries name their own runner; a typo becomes None (hostname
    decides), never a crash."""
    from modules.io import swarm as swarm_mod
    cfg = {"swarm": [{"url": "http://10.0.0.9:8080/v1", "model": "m1", "provider": "BigMoe"},
                     {"url": "http://10.0.0.9:8080/v1", "model": "m2", "provider": "typo"},
                     {"url": "http://10.0.0.9:8080/v1", "model": "m3"}]}
    out = swarm_mod.parse(cfg, None, "http://main:8080/v1", "main")
    assert [a["provider"] for a in out] == ["bigmoe", None, None]


def test_agent_provider_follows_the_endpoint(monkeypatch):
    """The thinking field is resolved per endpoint: the main base uses cfg
    'provider', a swarm base the entry's 'provider', everything else the hostname."""
    import threading
    from modules.core.agent import Agent

    class App:
        messages = []
        stop = threading.Event()
        reasoning = True
        model = "main"
        conns = []

        def set_head(self, blk, head):
            pass

    sent = []

    class Conn:
        def close(self):
            pass

    def fake_open_conn(base, body, conns=None, timeout=None, token=None):
        sent.append((base, body))
        return Conn(), None

    monkeypatch.setattr(inference, "open_conn", fake_open_conn)
    ag = Agent(App(), "http://main:8080/v1", 10, [], [],
               [{"url": "http://10.0.0.9:8080/v1", "model": "m1", "provider": "google"}], {},
               "", "", provider="bigmoe")
    ag.request({}, "https://generativelanguage.googleapis.com/v1beta/openai/", "g")
    ag.request({}, "http://10.0.0.9:8080/v1", "m1")
    ag.request({})
    assert [b.get("thinking") for _, b in sent] == [None, None, True]  # google swarm: field omitted
    assert ag.provider_of("https://my-proxy.local/v1") is None


def test_probe_detects_a_runner_without_model_args(monkeypatch):
    """A BigMoeOnEdge runner serves /models without args. No provider name is
    needed: /api/info answers, so the details come from there."""
    from modules.core import probe
    monkeypatch.setattr(probe, "_single_model_info", lambda b, m, c, t: None)
    monkeypatch.setattr(probe, "_models_info", lambda b, m, c, t: probe.default_info(c))
    monkeypatch.setattr(probe, "_bigmoe_model_info", lambda b, m, c, t: {"via": "detected"})
    assert probe.fetch_model_info("http://10.0.0.9:8080/v1", "m", 8192) == {"via": "detected"}


def test_probe_keeps_the_fallback_when_detection_fails(monkeypatch):
    from modules.core import probe
    monkeypatch.setattr(probe, "_single_model_info", lambda b, m, c, t: None)
    monkeypatch.setattr(probe, "_models_info", lambda b, m, c, t: None)
    monkeypatch.setattr(probe, "_bigmoe_model_info", lambda b, m, c, t: None)
    info = probe.fetch_model_info("http://10.0.0.9:8080/v1", "m", 8192)
    assert info["status"] == "hosted" and info["ctx"] == 8192


def test_named_provider_skips_the_detection(monkeypatch):
    """An explicit name is a decision, not a hint: no extra /api/info probe."""
    from modules.core import probe
    def boom(*a):
        raise AssertionError("detection must not run for a named provider")
    monkeypatch.setattr(probe, "_single_model_info", lambda b, m, c, t: None)
    monkeypatch.setattr(probe, "_bigmoe_model_info", boom)
    monkeypatch.setattr(probe, "_models_info", lambda b, m, c, t: probe.default_info(c))
    assert probe.fetch_model_info("http://10.0.0.9:8080/v1", "m", 8192, None, "openai")["ctx"] == 8192
