"""Inference-server probes: model details (llama.cpp model_manager API) and thinking
detection. Pure functions of (base, model) - no App state, testable against a mock server."""
import http.client
import json
import re
import time
from urllib.parse import urlparse
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
def fetch_model_info(base, model, ctx_fallback, token=None):
    """Model details from the llama.cpp model_manager API: context window (--ctx-size),
    reasoning effort, status and sampling parameters. Fallback: ctx_fallback, '' for the rest.
    token = optional per-server access token."""
    info = default_info(ctx_fallback)
    try:
        c, u, token = _conn(base, 3, token)
        try:
            c.request("GET", u.path.rstrip("/") + "/models", headers=_hdrs(token))
            data = json.loads(c.getresponse().read())
        finally:
            c.close()  # exception path (bad JSON, partial response) must not leak the socket (F3)
        for m in data.get("data") or []:
            if m.get("id") != model:
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
        pass
    return info
def detect_thinking(base, model, token=None):
    """Probe whether the model emits reasoning when thinking is requested (one retry)."""
    for _ in range(2):
        try:
            c, u = _conn(base, 30)
            try:
                c.request("POST", u.path.rstrip("/") + "/chat/completions",
                          json.dumps({"model": model, "thinking": True, "max_tokens": 16,
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
def info_text(model, info):
    """Human-readable model details for the /model command (like the old model_info.sh)."""
    return "\n".join(f"{k:<16}: {v}" for k, v in [
        ("ID", model), ("Status", info["status"] or "-"), ("Reasoning effort", info["effort"] or "-"),
        ("ctx-size", info["ctx"]), ("temperature", info["temp"] or "-"), ("top-k", info["top_k"] or "-"),
        ("top-p", info["top_p"] or "-"), ("min-p", info["min_p"] or "-"),
        ("architecture", info["arch"] or "-"), ("file", info["file"] or "-"),
        ("gpu-layers", info["gpu_layers"] or "-"), ("threads", info["threads"] or "-"),
        ("flash-attn", info["flash_attn"] or "-"), ("repeat-penalty", info["repeat_penalty"] or "-"),
        ("presence-penalty", info["presence_penalty"] or "-"), ("kv-cache", info["kv_cache"] or "-"),
        ("created", info["created"] or "-")])
def resolve(cfg_think, base, model, ctx_fallback, on_done, token=None):
    """Run both probes in parallel (non-blocking startup); on_done(info, think) fires with
    the results. think is None when cfg 'thinking' is an explicit bool (no probe needed).
    token = optional per-server access token."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        f_info = ex.submit(fetch_model_info, base, model, ctx_fallback, token)
        f_think = None if isinstance(cfg_think, bool) else ex.submit(detect_thinking, base, model, token)
        info = f_info.result()
        think = f_think.result() if f_think else None
    on_done(info, think)
