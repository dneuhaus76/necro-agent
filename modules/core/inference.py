"""NECRO inference HTTP layer: connection handling + SSE stream parsing.
Pure functions of (base, body) - no App state; the caller passes the connection
list so abort() can close in-flight requests (main stream + swarm sub-requests)."""
import http.client, json, os
from urllib.parse import urlparse
from modules.core import providers
class HTTPError(RuntimeError):
    """Non-200 response. status: the HTTP status code (4xx = definitive
    rejection -> non-retryable; 5xx = transient server fault -> retryable,
    see Agent._retry)."""
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status
def token_for(base, auth):
    """Optional per-server access token. Priority: env NECRO_API_TOKEN (global
    override, e.g. for OpenRouter via NECRO_BASE_URL), then cfg 'auth'
    ({url or host:port: token}). None when nothing matches (no auth header sent)
    - the token is a nice-to-have, not a requirement."""
    env = os.environ.get("NECRO_API_TOKEN")
    if env:
        return env
    if not isinstance(auth, dict):
        return None
    u = urlparse(base)
    host = f"{u.hostname}:{u.port}" if u.port else u.hostname
    for k, v in auth.items():
        if k in (base, u.hostname, host) and isinstance(v, str):
            return v
    return None
def open_conn(base, body, conns=None, timeout=None, token=None):
    """POST /chat/completions and return (conn, response). On error the connection
    is closed and dropped from conns; the exception propagates to the caller.
    timeout = per-socket-operation timeout in s, REQUIRED: cfg 'step_timeout_s' /
    NECRO_STEP_TIMEOUT_S owns the value. A default here would be a second copy of
    that config value and drift from it (the 900 s relic was exactly such a copy).
    token = optional access token sent as 'Authorization: Bearer <token>'."""
    if not timeout or timeout <= 0:
        raise ValueError(f"open_conn: timeout must be > 0 s, got {timeout!r}")
    u = urlparse(base)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=timeout)
    if conns is not None:
        conns.append(conn)  # tracked so abort() can close it
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        conn.request("POST", u.path.rstrip("/") + "/chat/completions", json.dumps(body), headers)
        r = conn.getresponse()
        if r.status != 200:
            raise HTTPError(f"HTTP {r.status}: {r.read(500).decode(errors='replace')}", status=r.status)
    except Exception:
        if conns is not None:
            try:
                conns.remove(conn)
            except ValueError:
                pass
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn, r
def thinking_value(base, on, name=None):
    """Provider-specific 'thinking' request field - see core.providers (llama.cpp
    takes a bool, DeepSeek a struct, Google rejects the field -> None = omit).
    name = explicit provider (cfg 'provider' / per-agent 'provider')."""
    return providers.thinking_field(providers.resolve(base, name), on)
def call_extra(tc):
    """Provider payload of one tool call that must be echoed back verbatim.
    Google's OpenAI-compatible endpoint returns the thought signature as
    extra_content.google.thought_signature; a replayed function call without it
    is rejected with HTTP 400 'Function call is missing a thought_signature'
    (Gemini 3, required for tools). Some servers put the signature directly on
    the call instead -> that is normalised into the same shape. {} = nothing."""
    out = {}
    ex = tc.get("extra_content")
    if isinstance(ex, dict) and ex:
        out["extra_content"] = json.loads(json.dumps(ex))  # detached copy
    sig = tc.get("thought_signature")
    if not sig:
        fn = tc.get("function")
        sig = fn.get("thought_signature") if isinstance(fn, dict) else None
    if isinstance(sig, str) and sig:
        out.setdefault("extra_content", {}).setdefault("google", {})["thought_signature"] = sig
    return out


def merge_extra(dst, src):
    """Merge a call_extra() dict into an accumulator (streaming: the payload can
    arrive in any chunk of the same tool call). Recursive, so an empty sub-dict in
    a later chunk never wipes an already captured signature."""
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge_extra(dst[k], v)
        else:
            dst[k] = v
    return dst


def assistant_msg(text, calls):
    """Assistant message of a tool-call step. calls: [{id, name, args, extra}] -
    'extra' (call_extra of the served response) goes back to the provider
    unchanged; without it Google rejects the next request (thought signature).
    Provider-neutral: for local servers 'extra' is empty and nothing is added."""
    m = {"role": "assistant", "content": text or None}
    if calls:
        m["tool_calls"] = []
        for c in calls:
            tc = {"id": c.get("id") or "", "type": "function",
                  "function": {"name": c.get("name") or "", "arguments": c.get("args") or "{}"}}
            tc.update(c.get("extra") or {})
            m["tool_calls"].append(tc)
    return m


def sse_events(resp):
    """Yield the parsed JSON objects of an OpenAI-compatible SSE stream (skips
    comment/keep-alive lines, [DONE] and malformed chunks)."""
    for raw in resp:
        raw = raw.strip()
        if not raw.startswith(b"data:") or raw[5:].strip() == b"[DONE]":
            continue
        try:
            yield json.loads(raw[5:])
        except ValueError:
            continue
