"""ctx_models - per-model context cap from necro.cfg.
A weak server is limited by prefill time, not by the window size: the cap must
win over the server-reported value, and an invalid entry must never cap to nothing."""
import pytest


@pytest.fixture
def necro_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    monkeypatch.setenv("NECRO_PEER_DIR", str(tmp_path / "peers"))
    import necro
    return necro


def test_exact_match_wins(necro_mod, monkeypatch):
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": 16000, "Qwen3.8-27B": 40000})
    assert necro_mod.ctx_cap("Qwen3-8", 131072) == 16000


def test_no_match_keeps_server_value(necro_mod, monkeypatch):
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": 16000})
    assert necro_mod.ctx_cap("Ornith1.5-35B", 131072) == 131072


def test_longest_prefix_wins(necro_mod, monkeypatch):
    """A quant suffix in the model id must still hit the short config key."""
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3": 8000, "Qwen3-8": 16000})
    assert necro_mod.ctx_cap("Qwen3-8-Q8_0.gguf", 131072) == 16000


def test_empty_cap_table_is_a_no_op(necro_mod, monkeypatch):
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {})
    assert necro_mod.ctx_cap("Qwen3-8", 32768) == 32768


@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_invalid_value_is_ignored(necro_mod, monkeypatch, bad):
    """Guard: a typo must not collapse the context to zero (that would spam compact)."""
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": bad})
    assert necro_mod.ctx_cap("Qwen3-8", 32768) == 32768


def test_numeric_string_is_accepted(necro_mod, monkeypatch):
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": "16000"})
    assert necro_mod.ctx_cap("Qwen3-8", 131072) == 16000


def test_config_validation():
    from modules.core.config import validate
    assert validate({"ctx_models": {"Qwen3-8": 16000, "_note": "comment"}}) == []
    issues = validate({"ctx_models": {"Qwen3-8": 0}})
    assert issues and "ctx_models['Qwen3-8']" in issues[0]


def test_app_starts_with_the_cap(necro_mod, monkeypatch):
    """The cap must be live before the first probe answer arrives."""
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": 16000})
    monkeypatch.setattr(necro_mod, "MODEL", "Qwen3-8")
    monkeypatch.setattr(necro_mod, "CTX_FALLBACK", 131072)
    assert necro_mod.ctx_cap(necro_mod.MODEL, necro_mod.CTX_FALLBACK) == 16000


def test_separator_and_case_variants_match(necro_mod, monkeypatch):
    """cfg 'Qwen3-8' must hit 'Qwen3.8-27B-Swift' and 'Qwen3_8-Q4': dash/dot/
    underscore/space and case differ between config and server, the model does not."""
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": 72000})
    for model in ("Qwen3-8", "qwen3.8-27B-Swift", "Qwen3_8-Q4_K_M.gguf", "QWEN3 8"):
        assert necro_mod.ctx_cap(model, 131072) == 72000, model


def test_catch_all_key(necro_mod, monkeypatch):
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"*": 60000, "Qwen3-8": 72000})
    assert necro_mod.ctx_cap("Ornith1.5-35B", 131072) == 60000  # no specific entry -> catch-all
    assert necro_mod.ctx_cap("Qwen3.8-27B-Swift", 131072) == 72000  # specific wins


def test_cap_never_exceeds_the_server_window(necro_mod, monkeypatch):
    """A cap above the server window would request more than the server can hold."""
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": 200000})
    assert necro_mod.ctx_cap("Qwen3-8", 131072) == 131072
    assert necro_mod.ctx_cap("Qwen3-8", 0) == 200000  # unknown window: the cap applies


def test_comment_keys_are_ignored(necro_mod, monkeypatch):
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"_note": "report", "_doc": 5})
    assert necro_mod.ctx_cap("Qwen3-8", 131072) == 131072
    assert necro_mod.ctx_cap_key("Qwen3-8") is None


def test_cap_key_is_reported_for_the_ui(necro_mod, monkeypatch):
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": 72000})
    assert necro_mod.ctx_cap_key("Qwen3.8-27B-Swift") == "Qwen3-8"  # shown in status line
    assert necro_mod.ctx_cap_key("Ornith1.5-35B") is None


def test_status_text_shows_the_active_cap(necro_mod, monkeypatch):
    """A configured cap that never matches must be visible, not silent."""
    a = object.__new__(necro_mod.App)
    a.ctx, a.ctx_used, a.ctx_est = 72000, 0, 0
    a.ctx_capped_key = "Qwen3-8"
    a.model, a.turns, a.step, a.busy, a.last_completion_tokens = "Qwen3.8-27B-Swift", 0, 0, False, 0
    assert "cap ctx_models['Qwen3-8']" in a.status_text()
    a.ctx_capped_key = None
    assert "server value" in a.status_text()


def test_set_ctx_remembers_the_key(necro_mod, monkeypatch):
    monkeypatch.setattr(necro_mod, "CTX_MODELS", {"Qwen3-8": 72000})
    a = object.__new__(necro_mod.App)
    a.model = "Qwen3.8-27B-Swift"
    assert a.set_ctx(131072) == 72000
    assert a.ctx_capped_key == "Qwen3-8"
