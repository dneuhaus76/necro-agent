"""Swarm agent logic: config parsing, name lookup, availability check.
Pure functions of the agent list - no App state, unit-testable without a server."""
import difflib
import os
import time
from collections import Counter
from urllib.parse import urlparse
def from_env(v, base):
    """NECRO_SWARM 'model=url;model=url' -> [{url, model}] (empty url = base)."""
    out = []
    for part in v.split(";"):
        if not part.strip():
            continue
        model, _, url = part.partition("=")
        model, url = model.strip(), url.strip()
        if model:
            out.append({"url": url or base, "model": model})
    return out
def parse(cfg, env, base, model):
    """Swarm list from env (NECRO_SWARM) or cfg. The main model (same url+model) is
    dropped: delegating to itself would be a self-call."""
    if env is not None:
        agents = from_env(env, base)
    else:
        agents = [{"url": a.get("url", base), "model": a["model"]}
                  for a in (cfg.get("swarm") or []) if isinstance(a, dict) and a.get("model")]
    return [a for a in agents if not (a["url"] == base and a["model"] == model)]
def resolve(cfg, swarm_env, remote, base, model):
    """swarm is OFF by default - active only via NECRO_SWARM (explicit env list,
    empty list = explicit off) or when remote mode is on (cfg 'swarm' list)."""
    if swarm_env is not None:
        return parse(cfg, swarm_env, base, model)
    if remote:
        return parse(cfg, None, base, model)
    return []
def agents_str(swarm):
    """Agent list for tool descriptions: name + url (unique identity), so the model can
    pick the right agent and see which server it runs on. Duplicate names are shown as
    'name@host' - the only unambiguous spelling (see find)."""
    dup = {n for n, c in Counter(a["model"] for a in swarm).items() if c > 1}
    return "; ".join(
        (f"{a['model']}@{urlparse(a['url']).hostname}" if a["model"] in dup else a["model"])
        + f" ({a['url']})" for a in swarm)
def find(name, swarm, base, model):
    """Resolve an agent by model name (exact match, then 'name@host' to disambiguate
    duplicate names, else closest by ratio). Returns (agent, None) or (None, error).
    the 'self' target (local main model) is gone - delegating to the
    own server+model was rarely useful and confused the tool descriptions."""
    if "@" in name:  # duplicate model names: 'name@host' picks the right server
        mname, _, host = name.partition("@")
        for a in swarm:
            if a["model"] == mname and urlparse(a["url"]).hostname == host:
                return a, None
    hits = [a for a in swarm if a["model"] == name]
    if len(hits) > 1:  # ambiguous: force the 'name@host' spelling
        return None, (f"error: swarm model '{name}' is ambiguous ({len(hits)} servers) - "
                      f"use 'name@host', e.g. {name}@" + urlparse(hits[0]["url"]).hostname)
    if hits:
        return hits[0], None
    best = max(swarm, key=lambda a: difflib.SequenceMatcher(None, name, a["model"]).ratio()) \
        if swarm else None
    err = f"error: unknown swarm model '{name}' (available: {', '.join(a['model'] for a in swarm)})"
    if best and difflib.SequenceMatcher(None, name, best["model"]).ratio() > 0.6:
        err += f" - did you mean '{best['model']}'?"
    return None, err
CHECK_ATTEMPTS = 3  # one GET /models can fail transiently - retry before marking offline
CHECK_RETRY_DELAY = 0.5  # s between attempts (worst case per agent: ~10 s, background thread)
def _probe(url, token):
    """One availability ping (GET /models, 3 s timeout); raises on any failure."""
    import http.client
    u = urlparse(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    c = cls(u.hostname, u.port, timeout=3)
    try:
        h = {}
        if token:
            h["Authorization"] = f"Bearer {token}"
        c.request("GET", u.path.rstrip("/") + "/models", headers=h)  # {} ok; None breaks py3.13 http.client
        c.getresponse().read()
    finally:
        c.close()  # exception path (bad JSON, partial response) must not leak the socket (F3)
def check_once(swarm, auth=None):
    """One availability pass: {(url, model): bool}. keyed by url+model, not the
    model name alone - duplicate names on different hosts must not share one status.
    Each agent is probed up to CHECK_ATTEMPTS times: a single transient
    network hiccup must not flip the status to 'off' for the whole 30 s cycle.
    auth = optional cfg 'auth' {server: token}."""
    from modules.core.inference import token_for
    up = {}
    for a in swarm:
        t = token_for(a["url"], auth)
        ok = False
        for i in range(CHECK_ATTEMPTS):
            try:
                _probe(a["url"], t)
                ok = True
                break
            except Exception:
                if i < CHECK_ATTEMPTS - 1:
                    time.sleep(CHECK_RETRY_DELAY)
        up[(a["url"], a["model"])] = ok
    return up
def workers(up, swarm, base=4, cap=8):
    """parallel tool-call workers scale with how many (good) models are online.
    base workers always; +1 per currently-up swarm agent, capped at cap. Pure function
    of the availability map - testable without a server."""
    online = sum(1 for a in swarm if up.get((a["url"], a["model"])))
    return min(cap, base + online)
def check_loop(swarm, on_change=None, delay_env="NECRO_SWARM_CHECK", default_delay=30, auth=None):
    """Availability check (GET /models, 3 s timeout) in a loop; on_change(up) fires when
    an agent's status flips. Runs forever - start it in a daemon thread.
    auth = optional cfg 'auth' {server: token}."""
    up = {}
    while True:
        new = check_once(swarm, auth)
        if new != up:  # compare first: on_change must not fire on an unchanged status
            up = new
            if on_change:
                on_change(up)
        try:
            delay = max(1, int(os.environ.get(delay_env, str(default_delay))))
        except ValueError:
            delay = default_delay  # a bad env var must not kill the daemon thread silently
        time.sleep(delay)
