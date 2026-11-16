"""Provider profiles: one table decides what a provider needs differently
(thinking field, model-info API, key header). A proxy that hides the hostname is
handled by an explicit override (cfg 'providers' / env NECRO_PROVIDERS)."""
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


def test_override_wins_over_the_hostname():
    """Proxy in front of Gemini: the hostname says nothing, the override decides."""
    base = "https://my-proxy.local/v1"
    prof = providers.resolve(base, {providers.host_of(base): "google"})
    assert prof is providers.GOOGLE
    assert inference.thinking_value(base, True, {"my-proxy.local": "google"}) is None


def test_override_by_full_url_and_port():
    assert providers.resolve("http://10.0.0.5:8080/v1", {"10.0.0.5:8080": "deepseek"}).name == "deepseek"
    assert providers.resolve("http://10.0.0.5:8080/v1", {"http://10.0.0.5:8080/v1": "deepseek"}).name == "deepseek"


def test_invalid_override_falls_back_silently():
    assert providers.resolve("http://127.0.0.1:8080/v1", {"127.0.0.1": "typo"}).name == "openai"
    assert providers.resolve("http://127.0.0.1:8080/v1", "nonsense").name == "openai"


def test_native_headers_use_the_key_header_only_for_native_apis():
    h = providers.native_headers(providers.GOOGLE, "SECRET")
    assert h["x-goog-api-key"] == "SECRET" and "Authorization" not in h
    h = providers.native_headers(providers.OPENAI, "SECRET")
    assert h["Authorization"] == "Bearer SECRET"


def test_cfg_and_env_overrides():
    assert providers.cfg_overrides({"providers": {"proxy.local": "GOOGLE", "x": 3}}) == {"proxy.local": "google"}
    assert providers.cfg_overrides({"providers": "no dict"}) == {}
    assert providers.cfg_overrides({}) == {}


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("NECRO_PROVIDERS", "a.local=google; b.local=typo; =google; c.local=deepseek")
    assert providers.env_overrides() == {"a.local": "google", "c.local": "deepseek"}


def test_probe_dispatches_by_profile(monkeypatch):
    """fetch_model_info must reach the native loader for the google profile only."""
    calls = []
    monkeypatch.setitem(probe.NATIVE_INFO, "google", lambda m, c, t: calls.append((m, c, t)) or {"native": 1})
    assert probe.fetch_model_info(GOOGLE_OPENAI, "gemini-x", 8192, "K") == {"native": 1}
    assert calls == [("gemini-x", 8192, "K")]
    assert probe.fetch_model_info("http://127.0.0.1:8080/v1", "local", 8192, None)["ctx"] == 8192


def test_probe_override_reaches_the_loader(monkeypatch):
    monkeypatch.setitem(probe.NATIVE_INFO, "google", lambda m, c, t: {"via": "override"})
    info = probe.fetch_model_info("https://proxy.local/v1", "gemini-x", 8192, None, {"proxy.local": "google"})
    assert info == {"via": "override"}


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
    monkeypatch.setattr(necro, "BASE", GOOGLE_OPENAI)
    out = a.model_info_text()
    assert "ctx-size" in out and "gpu-layers" not in out
