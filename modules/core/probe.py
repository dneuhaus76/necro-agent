"""Inference-server probes: model details (llama.cpp model_manager API) and thinking
detection. Pure functions of (base, model) - no App state, testable against a mock server."""
import http.client
import json
import re
import time
from urllib.parse import urlparse

from modules.core import inference, providers
def _conn(base, timeout, token=None):
    u = urlparse(base)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=timeout), u, token
def _hdrs(token=None):
    """Request headers; the optional access token goes as Bearer auth."""
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h
def default_info(ctx_fallback):
    """Empty model-info dict (all keys info_text() expects); the probe fills it in."""
    return {"ctx": ctx_fallback, "effort": "", "status": "", "temp": "", "top_k": "", "top_p": "", "min_p": "",
            "arch": "", "file": "", "gpu_layers": "", "threads": "", "flash_attn": "",
            "repeat_penalty": "", "presence_penalty": "", "kv_cache": "", "created": ""}
GOOGLE_API = "https://generativelanguage.googleapis.com/v1beta"  # native API (has /models)
def _google_get(path, token, timeout=5):
    """GET on the native Google API. The key travels in the provider's key header
    (x-goog-api-key), never in the URL (query strings end up in logs). An OAuth
    access token is retried as Bearer when the key header is rejected. Returns the
    JSON dict or None (unknown model, bad key, network error)."""
    prof = providers.GOOGLE
    as_key = providers.native_headers(prof, token)  # API key in the provider's header
    as_bearer = providers.native_headers(prof, None)  # same request, OAuth token instead
    if token:
        as_bearer["Authorization"] = f"Bearer {token}"
    for headers in (as_key, as_bearer) if token else (as_bearer,):
        try:
            c, _, _ = _conn(GOOGLE_API, timeout)
            try:
                c.request("GET", path, headers=headers)
                r = c.getresponse()
                body = r.read()
                if r.status == 200:
                    return json.loads(body)
                if r.status not in (401, 403):  # only auth errors are worth a second attempt
                    return None
            finally:
                c.close()
        except Exception:
            return None
    return None
def _google_model_id(model):
    """Native API id: 'models/x' and provider-prefixed 'google/x' both mean 'x'."""
    return model.rsplit("/", 1)[-1]
def _google_model_info(model, ctx_fallback, token):
    """Google's OpenAI-compatible endpoint has no /models, but the native API
    (GET /v1beta/models/<model>) reports the real input token limit plus the
    sampling defaults. Returns None when the lookup fails (caller uses the
    fallback, which then says so)."""
    m = _google_get(f"/v1beta/models/{_google_model_id(model)}", token)
    if not isinstance(m, dict):
        return None
    info = default_info(ctx_fallback)
    ver = m.get("version") or ""
    info["status"] = f"hosted ({ver})" if ver else "hosted"
    if isinstance(m.get("inputTokenLimit"), int):
        info["ctx"] = m["inputTokenLimit"]
    info["arch"] = "text->text"
    if m.get("thinking"):
        info["effort"] = "on"
    for key, field in (("temperature", "temp"), ("topP", "top_p"), ("topK", "top_k")):
        if m.get(key) is not None:
            info[field] = str(m[key])
    return info
def _hosted_fallback(base, model, ctx_fallback, reason=""):
    """Minimal info for hosted APIs whose /models endpoint is missing (e.g. the
    OpenAI-compatible Google endpoint) - the probe would otherwise leave /model
    empty. Only what is knowable without a working lookup is filled; a reason
    makes a failed lookup distinguishable from a genuinely sparse answer."""
    info = default_info(ctx_fallback)
    info["status"] = f"hosted - {reason}" if reason else "hosted"
    info["arch"] = "text->text"
    return info
NATIVE_INFO = {"google": _google_model_info}  # info_api -> loader (providers.Profile.info_api)
def fetch_model_info(base, model, ctx_fallback, token=None, overrides=None):
    """Model details from the llama.cpp model_manager API: context window (--ctx-size),
    reasoning effort, status and sampling parameters. Fallback: ctx_fallback, '' for the rest.
    token = optional per-server access token; overrides = cfg 'providers'."""
    info = default_info(ctx_fallback)
    prof = providers.resolve(base, overrides)
    if prof.info_api in NATIVE_INFO:  # no usable /models: the provider's native API answers
        return (NATIVE_INFO[prof.info_api](model, ctx_fallback, token)
                or _hosted_fallback(base, model, ctx_fallback, "native API lookup failed"))
    try:
        c, u, token = _conn(base, 3, token)
        try:
            c.request("GET", u.path.rstrip("/") + "/models", headers=_hdrs(token))
            r = c.getresponse()
            if r.status != 200:  # hosted API without a /models endpoint
                return _hosted_fallback(base, model, ctx_fallback)
            data = json.loads(r.read())
        finally:
            c.close()  # exception path (bad JSON, partial response) must not leak the socket (F3)
        for m in data.get("data") or []:
            if m.get("id") != model:
                continue
            if "context_window" in m:  # hosted API (e.g. DeepSeek): flat fields, no server args
                info["status"] = m.get("name") or ""
                if isinstance(m.get("context_window"), int):
                    info["ctx"] = m["context_window"]
                if e := (m.get("effort") or {}).get("default_level"):
                    info["effort"] = e
                info["arch"] = _arch_text({"input_modalities": m.get("input_modalities"),
                                           "output_modalities": m.get("output_modalities")})
                continue
            args = (m.get("status") or {}).get("args") or []
            def g(flag):  # value of a --flag in the server args, '' if absent or dangling
                i = args.index(flag) if flag in args else -1
                return args[i + 1] if 0 <= i < len(args) - 1 else ""
            info["status"] = (m.get("status") or {}).get("value", "")
            if v := g("--ctx-size"):  # value configured on the server right now
                try:
                    info["ctx"] = int(v)
                except ValueError:
                    pass  # malformed value - keep the fallback, don't lose the rest
            if mm := re.search(r'"reasoning_effort"\s*:\s*"([^"]+)"', " ".join(args)):
                info["effort"] = mm.group(1)  # reasoning effort (reasoning is thinking)
            info["temp"], info["top_k"] = g("--temperature"), g("--top-k")
            info["top_p"], info["min_p"] = g("--top-p"), g("--min-p")
            # extra details for /model (architecture, file, GPU/threads, penalties, KV cache, age)
            info["arch"] = _arch_text(m.get("architecture"))
            mfile = g("--model")  # the loaded .gguf path; 'source' is often just 'preset'
            if mfile:
                info["file"] = mfile.rsplit("/", 1)[-1]
            elif "/" in (m.get("source") or ""):  # fallback: a real path in 'source'
                info["file"] = m["source"].rsplit("/", 1)[-1]
            info["gpu_layers"], info["threads"] = g("--n-gpu-layers"), g("--threads")
            info["flash_attn"] = g("--flash-attn")
            info["repeat_penalty"], info["presence_penalty"] = g("--repeat-penalty"), g("--presence-penalty")
            if g("--cache-type-k"):  # KV cache quantization (k/v), '' when not set
                info["kv_cache"] = f"{g('--cache-type-k')}/{g('--cache-type-v')}"
            if m.get("created"):  # server-side model load timestamp -> human date
                info["created"] = time.strftime("%Y-%m-%d", time.gmtime(m["created"]))
    except Exception:
        return _hosted_fallback(base, model, ctx_fallback)  # no /models (e.g. Google): minimal info
    return info
def detect_thinking(base, model, token=None):
    """Probe whether the model emits reasoning when thinking is requested (one retry).
    Providers that reject the 'thinking' field (Google) are probed without it -
    the probe then just reports no reasoning output."""
    tv = inference.thinking_value(base, True)
    for _ in range(2):
        try:
            c, u, _ = _conn(base, 30, token)
            try:
                c.request("POST", u.path.rstrip("/") + "/chat/completions",
                          json.dumps({"model": model, **({} if tv is None else {"thinking": tv}), "max_tokens": 16,
                                      "messages": [{"role": "user", "content": "Say: OK"}]}),
                          _hdrs(token))
                msg = json.loads(c.getresponse().read())["choices"][0]["message"]
            finally:
                c.close()  # exception path must not leak the socket (F3)
            return bool((msg.get("reasoning_content") or "").strip())
        except Exception:
            time.sleep(1)  # server may still be loading the model
    return False  # both probes failed: assume no reasoning output
def _arch_text(a):
    """The model_manager 'architecture' field is a modalities dict (or a plain string)."""
    if isinstance(a, dict):
        i = "/".join(a.get("input_modalities") or []) or "?"
        o = "/".join(a.get("output_modalities") or []) or "?"
        return f"{i}->{o}"
    return a or ""
# label -> model-info key, in display order (the canonical row set of /model)
FIELDS = (("ctx-size", "ctx"), ("temperature", "temp"), ("top-k", "top_k"), ("top-p", "top_p"),
          ("min-p", "min_p"), ("architecture", "arch"), ("file", "file"), ("gpu-layers", "gpu_layers"),
          ("threads", "threads"), ("flash-attn", "flash_attn"), ("repeat-penalty", "repeat_penalty"),
          ("presence-penalty", "presence_penalty"), ("kv-cache", "kv_cache"), ("created", "created"))
def info_text(model, info, prof=None):
    """Human-readable model details for the /model command. Which rows are shown
    depends on the provider profile (Profile.detail): a hosted API has no GPU
    layers, threads or KV cache, so those rows are left out instead of "-"."""
    prof = prof or providers.OPENAI
    rows = [("ID", model), ("Status", info["status"] or "-"), ("Reasoning effort", info["effort"] or "-")]
    rows += [(label, info[key] or "-") for label, key in FIELDS if key in prof.detail]
    return "\n".join(f"{k:<16}: {v}" for k, v in rows)
def resolve(cfg_think, base, model, ctx_fallback, on_done, token=None, overrides=None):
    """Run both probes in parallel (non-blocking startup); on_done(info, think) fires with
    the results. think is None when cfg 'thinking' is an explicit bool (no probe needed).
    token = optional per-server access token."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        f_info = ex.submit(fetch_model_info, base, model, ctx_fallback, token, overrides)
        f_think = None if isinstance(cfg_think, bool) else ex.submit(detect_thinking, base, model, token)  # noqa
        info = f_info.result()
        think = f_think.result() if f_think else None
    on_done(info, think)
