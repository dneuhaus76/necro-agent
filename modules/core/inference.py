"""NECRO inference HTTP layer: connection handling + SSE stream parsing.
Pure functions of (base, body) - no App state; the caller passes the connection
list so abort() can close in-flight requests (main stream + swarm sub-requests)."""
import http.client, json
from urllib.parse import urlparse
def token_for(base, auth):
    """Optional per-server access token from cfg 'auth' ({url or host:port: token}).
    None when the key is absent (no auth header sent) - the token is a nice-to-have,
    not a requirement."""
    if not isinstance(auth, dict):
        return None
    u = urlparse(base)
    host = f"{u.hostname}:{u.port}" if u.port else u.hostname
    for k, v in auth.items():
        if k in (base, u.hostname, host) and isinstance(v, str):
            return v
    return None
def open_conn(base, body, conns=None, timeout=900, token=None):
    """POST /chat/completions and return (conn, response). On error the connection
    is closed and dropped from conns; the exception propagates to the caller.
    timeout = per-socket-operation timeout (a hung stream must not block the turn
    forever - now configurable via cfg 'step_timeout' / NECRO_STEP_TIMEOUT).
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
            raise RuntimeError(f"HTTP {r.status}: {r.read(500).decode(errors='replace')}")
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
