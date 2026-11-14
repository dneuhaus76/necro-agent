"""NECRO inference HTTP layer: connection handling + SSE stream parsing.
Pure functions of (base, body) - no App state; the caller passes the connection
list so abort() can close in-flight requests (main stream + swarm sub-requests)."""
import http.client, json, os
from urllib.parse import urlparse
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
def open_conn(base, body, conns=None, timeout=300, token=None):
    """POST /chat/completions and return (conn, response). On error the connection
    is closed and dropped from conns; the exception propagates to the caller.
    timeout = per-socket-operation timeout (a hung stream must not block the turn
    forever - now configurable via cfg 'step_timeout' / NECRO_STEP_TIMEOUT). The
    default (300 s) matches the STEP_TIMEOUT default so a call without an explicit
    timeout does not silently get 3x longer than intended (was 900 s, a relic of
    the pre-STEP_TIMEOUT hardcode).
    token = optional access token sent as 'Authorization: Bearer <token>'."""
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
def thinking_value(base, on):
    """Provider-specific 'thinking' request field. llama.cpp takes a bool;
    DeepSeek (api.deepseek.com) requires a struct: {"type": "enabled"|"disabled"}.
    Anything else (OpenAI-compatible servers) keeps the plain bool. Google
    (generativelanguage.googleapis.com) rejects the unknown field with HTTP 400
    -> None, so the caller omits it entirely."""
    h = urlparse(base).hostname or ""
    if "googleapis.com" in h:
        return None
    if "deepseek.com" in h:
        return {"type": "enabled" if on else "disabled"}
    return bool(on)
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
