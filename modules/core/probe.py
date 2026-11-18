"""Inference-server probes: model details (llama.cpp model_manager API) and thinking
detection. Pure functions of (base, model) - no App state, testable against a mock server."""
import http.client
import json
import re
import time
from urllib.parse import quote, urlparse

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
def _google_model_info(base, model, ctx_fallback, token):
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
def _bigmoe_model_info(base, model, ctx_fallback, token):
    """BigMoeOnEdge runner: no /models, the live model details sit on GET /api/info
    (model name, arch, n_ctx, threads, load time). Returns None when the lookup
    fails (caller uses the fallback, which then says so)."""
    try:
        c, u, _ = _conn(base, 3, token)
        try:
            # the runner serves the endpoint at the root ('/api/info'); the base path
            # ('/v1/api/info') is the fallback for a proxied base url
            paths = [u.path.rstrip("/") + "/api/info"]
            if paths[0] != "/api/info":
                paths.insert(0, "/api/info")
            data = None
            for path in paths:
                c.request("GET", path, headers=_hdrs(token))
                r = c.getresponse()
                if r.status == 200:
                    data = json.loads(r.read())
                    break
                r.read()  # drain the error body, the connection stays usable
            if data is None:
                return None
        finally:
            c.close()
    except Exception:
        return None
    m = data.get("model") or {}
    info = default_info(ctx_fallback)
    info["status"] = data.get("state") or "hosted"
    if isinstance(m.get("n_ctx"), int):
        info["ctx"] = m["n_ctx"]
    info["arch"] = m.get("arch") or ""
    info["file"] = (m.get("path") or "").rsplit("/", 1)[-1]
    if m.get("compute_threads"):
        info["threads"] = str(m["compute_threads"])
    if m.get("load_s"):
        info["created"] = time.strftime("%Y-%m-%d", time.gmtime(time.time() - m["load_s"]))
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
# info_api -> loader (providers.Profile.info_api): a provider without a usable
# /models endpoint points here. One row per provider - a new runner is one
# Profile + one loader, nothing else.
NATIVE_INFO = {"google": _google_model_info, "bigmoe": _bigmoe_model_info}
def _arg(args, flag):
    """Value after --flag in a server arg list, '' when absent or dangling."""
    i = args.index(flag) if flag in args else -1
    return args[i + 1] if 0 <= i < len(args) - 1 else ""
def _reasoning(args):
    """Server-side reasoning state from the llama.cpp launch args. An explicit
    effort level (--chat-template-kwargs '{"reasoning_effort":"low"}') wins,
    else the switch decides: 'off' when --reasoning is false/none/off or
    --reasoning-budget is 0 (no tokens left for thinking), 'on' when the server
    enables it explicitly. '' = the server says nothing about reasoning, so the
    caller keeps the request toggle as the display fallback.
    A preset started with `reasoning = false` emits no reasoning_content even
    when the request asks for it - reporting 'on' there would be a lie."""
    if m := re.search(r'"reasoning_effort"\s*:\s*"([^"]+)"', " ".join(args)):
        return m.group(1)  # effort level rather than a switch (low/medium/high)
    if v := _arg(args, "--reasoning-effort"):
        return v
    switch, budget = _arg(args, "--reasoning").lower(), _arg(args, "--reasoning-budget")
    if switch in ("false", "none", "off"):
        return "off"
    if budget:
        try:
            if int(budget) == 0:
                return "off"
        except ValueError:
            budget = ""  # malformed value: no claim in either direction
    return "on" if switch or budget else ""
CTX_KEYS = ("context_window", "context_length", "max_context_length")  # DeepSeek, OpenRouter
SINGLE_PATHS = ("/models/{model}", "/model/{model}")  # OpenAI single model | OpenRouter


def _flat_info(base, model, ctx_fallback, m):
    """Details from a hosted API's flat model object (no server args). The list
    (/models) and the single lookup (/model, /models/<id>) answer the same shape,
    so both paths share this mapping."""
    info = default_info(ctx_fallback)
    info["status"] = m.get("name") or ""
    if isinstance(m.get("context_window"), int):  # DeepSeek list shape
        info["ctx"] = m["context_window"]
    else:
        for key in CTX_KEYS:  # OpenRouter publishes the window as context_length
            if isinstance(m.get(key), int):
                info["ctx"] = m[key]
                break
    if e := (m.get("effort") or {}).get("default_level"):
        info["effort"] = e
    arch = m.get("architecture")
    info["arch"] = _arch_text(arch if isinstance(arch, dict)
                              else {"input_modalities": m.get("input_modalities"),
                                    "output_modalities": m.get("output_modalities")})
    for key, field in (("temperature", "temp"), ("top_p", "top_p"), ("top_k", "top_k")):
        if (m.get("default_parameters") or {}).get(key) is not None:
            info[field] = str(m["default_parameters"][key])
    if m.get("created"):
        info["created"] = time.strftime("%Y-%m-%d", time.gmtime(m["created"]))
    return info


def _model_object(data, model):
    """The model object out of a single-lookup answer: OpenRouter wraps it in
    'data', OpenAI returns it bare. None when it is not the asked model."""
    m = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    if not isinstance(m, dict) or not m.get("id"):
        return None
    return m if m["id"] == model else None


def _single_model_info(base, model, ctx_fallback, token):
    """Details from a single-model lookup. OpenAI answers on GET /models/<id>,
    OpenRouter on GET /model/<id> (singular, above its OpenAI-compatible routes).
    Returns None when neither path answers with the asked model."""
    root, quoted = urlparse(base).path.rstrip("/"), quote(model, safe="/:@")
    for path in (p.format(model=quoted) for p in SINGLE_PATHS):
        try:  # one connection per path: a keep-alive close must not skip the second path
            c, _, _ = _conn(base, 3, token)
            try:
                c.request("GET", root + path, headers=_hdrs(token))
                r = c.getresponse()
                body = r.read()
            finally:
                c.close()
        except Exception:
            return None  # host does not answer: the second route would fail the same way
        if r.status != 200:  # this provider has no single-model route under this path
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if m := _model_object(data, model):
            return _flat_info(base, model, ctx_fallback, m)
    return None


def _models_info(base, model, ctx_fallback, token):
    """Details from the OpenAI-compatible GET /models. None when the endpoint is
    missing or fails; a dict with only the fallback ctx when the server answers
    but publishes no model args (a runner without a model_manager API)."""
    info = default_info(ctx_fallback)
    try:
        c, u, token = _conn(base, 3, token)
        try:
            c.request("GET", u.path.rstrip("/") + "/models", headers=_hdrs(token))
            r = c.getresponse()
            if r.status != 200:  # hosted API without a /models endpoint
                return None
            data = json.loads(r.read())
        finally:
            c.close()  # exception path (bad JSON, partial response) must not leak the socket (F3)
        for m in data.get("data") or []:
            if m.get("id") != model:
                continue
            if any(key in m for key in CTX_KEYS):  # hosted API (DeepSeek, OpenRouter): flat fields
                return _flat_info(base, model, ctx_fallback, m)
            args = (m.get("status") or {}).get("args") or []
            def g(flag):  # value of a --flag in the server args, '' if absent or dangling
                return _arg(args, flag)
            info["status"] = (m.get("status") or {}).get("value", "")
            if v := g("--ctx-size"):  # value configured on the server right now
                try:
                    info["ctx"] = int(v)
                except ValueError:
                    pass  # malformed value - keep the fallback, don't lose the rest
            if eff := _reasoning(args):  # server truth, not the request toggle
                info["effort"] = eff
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
        return None  # no /models (e.g. Google native, dead server): the caller falls back
    return info


def has_details(info, ctx_fallback):
    """Did /models publish real details, or only the fallback context?"""
    return bool(info) and (bool(info["arch"]) or info["ctx"] != ctx_fallback)


def fetch_model_info(base, model, ctx_fallback, token=None, name=None):
    """Model details: context window, reasoning effort, status, sampling parameters.
    Fallback: ctx_fallback, '' for the rest. token = optional per-server access token;
    name = explicit provider name (cfg 'provider' / env NECRO_PROVIDER).

    A named provider with a native API answers directly (Google). Otherwise the
    single-model lookup is tried first (OpenAI /models/<id>, OpenRouter /model/<id>),
    then the /models list. Publishes the server no details (a BigMoeOnEdge runner
    serves /models without args), the runner's own /api/info is asked before giving
    up - so a local IP needs no provider name at all."""
    prof = providers.resolve(base, name)
    if prof.info_api in NATIVE_INFO:  # no usable /models: the provider's native API answers
        return (NATIVE_INFO[prof.info_api](base, model, ctx_fallback, token)
                or _hosted_fallback(base, model, ctx_fallback, "native API lookup failed"))
    info = _single_model_info(base, model, ctx_fallback, token)
    if has_details(info, ctx_fallback):
        return info
    info = _models_info(base, model, ctx_fallback, token) or info
    if has_details(info, ctx_fallback):
        return info
    if name is None:  # no name given: the runner may answer on its own API
        if det := _bigmoe_model_info(base, model, ctx_fallback, token):
            return det
    return info or _hosted_fallback(base, model, ctx_fallback)


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
def resolve(cfg_think, base, model, ctx_fallback, on_done, token=None, name=None):
    """Run both probes in parallel (non-blocking startup); on_done(info, think) fires with
    the results. think is None when cfg 'thinking' is an explicit bool (no probe needed).
    token = optional per-server access token; name = explicit provider name."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        f_info = ex.submit(fetch_model_info, base, model, ctx_fallback, token, name)
        f_think = None if isinstance(cfg_think, bool) else ex.submit(detect_thinking, base, model, token)  # noqa
        info = f_info.result()
        think = f_think.result() if f_think else None
    on_done(info, think)
