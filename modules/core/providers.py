"""Provider profiles: the few places where OpenAI-compatible endpoints differ.

One table instead of hostname checks scattered over the HTTP layer - a new
provider is one row, not edits in three files. Deliberately data-driven: no
class per provider, no plugin loading (each provider differs in 2-3 fields, so
a base class would be pure ceremony).
"""
import os
from typing import NamedTuple
from urllib.parse import urlparse

# detail keys of the model info (core.probe.FIELDS) that are worth showing per
# provider: a hosted API has no GPU layers, threads or KV cache - printing those
# as "-" is noise. Which rows /model shows is a provider trait, not a UI decision.
LOCAL_DETAIL = ("ctx", "temp", "top_k", "top_p", "min_p", "arch", "file", "gpu_layers", "threads",
                "flash_attn", "repeat_penalty", "presence_penalty", "kv_cache", "created")
HOSTED_DETAIL = ("ctx", "temp", "top_k", "top_p", "arch")

class Profile(NamedTuple):
    """What a provider needs differently. Defaults describe a plain
    OpenAI-compatible server (llama.cpp, vLLM, OpenRouter, ...)."""
    name: str
    hosts: tuple = ()          # hostname suffixes that select this profile
    thinking: str = "bool"     # request field for thinking: bool | struct | omit
    info_api: str = ""        # model-details API: "" = OpenAI /models | google | ...
    key_header: str = ""      # API key header on the *native* API ("" = Bearer)
    detail: tuple = LOCAL_DETAIL  # model-info rows /model shows for this provider
    notes: str = ""

OPENAI = Profile("openai", notes="plain OpenAI-compatible /chat/completions + /models")
GOOGLE = Profile("google", hosts=("googleapis.com",), thinking="omit", info_api="google",
                 key_header="x-goog-api-key", detail=HOSTED_DETAIL,
                 notes="no /models on the openai endpoint, thinking field is rejected")
DEEPSEEK = Profile("deepseek", hosts=("deepseek.com",), thinking="struct",
                   detail=("ctx", "temp", "top_p", "arch"),
                   notes="thinking wants {'type': 'enabled'|'disabled'}, hosted")
PROVIDERS = (GOOGLE, DEEPSEEK)  # only hosts with quirks: everything else is OPENAI
BY_NAME = {p.name: p for p in (OPENAI, *PROVIDERS)}
def host_of(base):
    return (urlparse(base).hostname or "").lower()
def resolve(base, overrides=None):
    """Profile for a base URL. Priority: an explicit override from cfg 'providers'
    ({base url or hostname: provider name} - needed behind a proxy that hides the
    real host), then a hostname suffix match, then the OpenAI default."""
    if isinstance(overrides, dict):
        u = urlparse(base)
        host = u.hostname or ""
        for key in (base, host, f"{host}:{u.port}" if u.port else host):
            name = overrides.get(key)
            if isinstance(name, str) and name.lower() in BY_NAME:
                return BY_NAME[name.lower()]
    h = host_of(base)
    for p in PROVIDERS:
        if any(h == suffix or h.endswith("." + suffix) for suffix in p.hosts):
            return p
    return OPENAI
def thinking_field(profile_or_base, on):
    """Value of the 'thinking' request field, or None when the field must be
    omitted (Google rejects the unknown field with HTTP 400)."""
    p = profile_or_base if isinstance(profile_or_base, Profile) else resolve(profile_or_base)
    if p.thinking == "omit":
        return None
    if p.thinking == "struct":  # DeepSeek
        return {"type": "enabled" if on else "disabled"}
    return bool(on)
def native_headers(profile_or_base, token):
    """Headers for a provider's *native* (non-OpenAI) API. Chat requests stay on
    Bearer auth everywhere - only the native lookups use a key header."""
    p = profile_or_base if isinstance(profile_or_base, Profile) else resolve(profile_or_base)
    h = {"Content-Type": "application/json"}
    if token:
        h[p.key_header or "Authorization"] = token if p.key_header else f"Bearer {token}"
    return h
def cfg_overrides(cfg):
    """cfg 'providers' as a string->string dict (invalid entries are dropped, a
    typo must not break the probe)."""
    raw = (cfg or {}).get("providers")
    if not isinstance(raw, dict):
        return {}
    return {k: v.lower() for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
def env_overrides():
    """NECRO_PROVIDERS='host=name;host=name' - the env twin of cfg 'providers'."""
    raw = os.environ.get("NECRO_PROVIDERS") or ""
    out = {}
    for part in raw.split(";"):
        host, _, name = part.partition("=")
        if host.strip() and name.strip().lower() in BY_NAME:
            out[host.strip()] = name.strip().lower()
    return out
