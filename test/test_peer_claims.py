"""N226 parent-child peering: a source must not address a grandchild directly.
When my child X calls Y, X claims Y at its parent (/api/claim); the parent
excludes Y from its peer list (tool description + _tool_peer) until the claim
is released (Y's turn done) or goes stale (crashed claimer, TTL)."""
import json
import threading
import time
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def _clean_claims():
    from modules.io import api
    yield
    with api._PEER_CLAIMS_LOCK:
        api._PEER_CLAIMS.clear()


# ---------------------------------------------------------------- claims core
def test_claim_set_release_and_ttl():
    """claim -> excluded; release -> free; stale claim (TTL) -> free."""
    from modules.io import api
    api.peer_set_claim("C", "B", "claim")
    assert api.peer_excluded_ids() == {"C"}
    api.peer_set_claim("C", "B", "release")
    assert api.peer_excluded_ids() == set()
    api.peer_set_claim("D", "B", "claim")
    with api._PEER_CLAIMS_LOCK:
        # backdate the claim beyond the TTL (crashed claimer backstop)
        c, ts = api._PEER_CLAIMS["D"]
        api._PEER_CLAIMS["D"] = (c, ts - api.PEER_CLAIM_TTL - 1)
    assert api.peer_excluded_ids() == set()
    # stale entry is REMOVED from the dict (not just filtered on read) - a
    # crashed claimer must not leak its entry forever (unbounded growth)
    with api._PEER_CLAIMS_LOCK:
        assert "D" not in api._PEER_CLAIMS


def test_claim_ttl_tracks_turn_timeout():
    """F1: PEER_CLAIM_TTL must outlive the longest peer turn, else a live claim
    goes stale mid-flight. PeerAPI.__init__ re-derives it as peer_turn_timeout + 600."""
    from modules.io import api
    old = api.PEER_CLAIM_TTL
    try:
        # a long peer turn (3600 s) must not drop its own claim at 900 s
        api.PeerAPI(FakeApp(), host="127.0.0.1", port=0, peer_turn_timeout=3600)
        assert api.PEER_CLAIM_TTL >= 3600 + 600
    finally:
        api.PEER_CLAIM_TTL = old


def test_claim_ttl_keeps_floor_for_short_turns():
    """F1: a short peer_turn_timeout must not shrink the TTL below the 900 s floor
    (the crashed-claimer backstop stays meaningful even for quick turns)."""
    from modules.io import api
    old = api.PEER_CLAIM_TTL
    try:
        api.PeerAPI(FakeApp(), host="127.0.0.1", port=0, peer_turn_timeout=60)
        assert api.PEER_CLAIM_TTL >= 900  # floor kept, not 60 + 600 = 660
    finally:
        api.PEER_CLAIM_TTL = old


# ---------------------------------------------------------------- api level
class FakeApp:
    def __init__(self):
        self.lock = threading.RLock()
        self.busy = False
        self.messages = []
        self.scroll = 0
        self.dirty = True
        self.peer_tag = "📓"
        self.stop = threading.Event()
        self._peer_turn = False
        self._running = threading.Event()

    def add(self, kind, text="", color=None):
        self.messages.append({"role": kind, "content": text})
        return None

    def db_sync(self):
        pass

    def turn(self, chat_id=None, done=None):
        m = {"role": "assistant", "content": "ok"}
        self.messages.append(m)
        self._last_reply = m
        if done is not None:
            done.set()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_PEER_DIR", str(tmp_path / "peers"))
    from modules.io import api as api_mod
    app = FakeApp()
    p = api_mod.PeerAPI(app, host="127.0.0.1", port=0)
    return api_mod, p, app


def _claim(p, child, op, frm="B"):
    body = json.dumps({"child": child, "op": op, "from": frm}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{p.port}/api/claim", body,
        {"Content-Type": "application/json", "X-Necro-Token": p.token})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_api_claim_endpoint(api):
    """POST /api/claim records/releases a grandchild claim on the receiver."""
    api_mod, p, app = api
    status, body = _claim(p, "C", "claim")
    assert status == 200 and body["ok"] is True
    assert api_mod.peer_excluded_ids() == {"C"}
    status, _ = _claim(p, "C", "release")
    assert status == 200
    assert api_mod.peer_excluded_ids() == set()


def test_api_claim_bad_token_and_empty_child(api):
    api_mod, p, app = api
    body = json.dumps({"child": "C", "op": "claim"}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{p.port}/api/claim", body,
        {"Content-Type": "application/json", "X-Necro-Token": "wrong"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
    assert api_mod.peer_excluded_ids() == set()
    _claim(p, "", "claim")  # empty child id is ignored, not an error
    assert api_mod.peer_excluded_ids() == set()


def test_peer_claim_posts_to_parent(api, monkeypatch):
    """peer_claim() sends child/op to the parent's /api/claim endpoint."""
    api_mod, p, app = api
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        raise ConnectionError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    inst = json.loads(p.path.read_text())
    assert api_mod.peer_claim(inst, "C", "claim") is False  # fake urlopen raises
    assert "/api/claim" in captured["url"]
    assert captured["body"]["child"] == "C" and captured["body"]["op"] == "claim"
    assert captured["body"]["from"] == api_mod.OWN_ID


# ------------------------------------------------------------- agent level
def _agent_setup(api, monkeypatch, inst):
    api_mod, p, app = api
    from modules.core import agent as agent_mod
    monkeypatch.setattr(api_mod, "peer_instances", lambda: inst)
    monkeypatch.setattr(api_mod, "OWN_ID", "me")
    monkeypatch.setattr(api_mod, "peer_busy", lambda x: False)
    calls, claims = [], []

    def fake_prompt(inst, task, timeout=None, hops=1, budget_left=None):
        calls.append(inst["id"])
        return "done"

    def fake_claim(inst, child_id, op="claim", timeout=2):
        claims.append((inst["id"], child_id, op))
        return True

    monkeypatch.setattr(api_mod, "peer_prompt", fake_prompt)
    monkeypatch.setattr(api_mod, "peer_health", lambda x, timeout=2: (False, None))
    monkeypatch.setattr(api_mod, "peer_claim", fake_claim)
    # peer_max_hops=2: the claim tests run a peer turn that calls a peer (hop 2)
    ag = agent_mod.Agent(app, "http://x/v1", 5, [], [], [], {}, "", "", peer_max_hops=2)
    return api_mod, app, ag, calls, claims


def test_tool_peer_excludes_grandchild(api, monkeypatch):
    """source (local turn): a grandchild claimed by my child is not routed to
    and an explicit target gets a clear error."""
    inst = [{"id": "A", "pid": 1, "cwd": "/a", "port": 1, "token": "t"},
            {"id": "B", "pid": 2, "cwd": "/b", "port": 2, "token": "t"},
            {"id": "C", "pid": 3, "cwd": "/c", "port": 3, "token": "t"}]
    api_mod, app, ag, calls, claims = _agent_setup(api, monkeypatch, inst)
    api_mod.peer_set_claim("C", "B", "claim")
    try:
        ag._tool_peer({"task": "t"}, None)
        assert calls[-1] == "A"  # C not picked, A (first idle) is
        r = ag._tool_peer({"task": "t", "instance": "C"}, None)
        assert "grandchild" in r
        # all claimed -> no candidate, the error names the excluded ids
        api_mod.peer_set_claim("A", "B", "claim")
        api_mod.peer_set_claim("B", "B", "claim")
        r = ag._tool_peer({"task": "t"}, None)
        assert r.startswith("error: no sibling") and "A, B, C" in r
    finally:
        for i in ("C", "A", "B"):
            api_mod.peer_set_claim(i, "B", "release")


def test_tool_peer_claims_at_parent_in_peer_turn(api, monkeypatch):
    """peer turn from A: calling B claims B at parent A before the call and
    releases it afterwards (even on error)."""
    inst = [{"id": "A", "pid": 1, "cwd": "/a", "port": 1, "token": "t"},
            {"id": "B", "pid": 2, "cwd": "/b", "port": 2, "token": "t"}]
    api_mod, app, ag, calls, claims = _agent_setup(api, monkeypatch, inst)
    app._peer_from, app._peer_hops = "A", 1
    ag._tool_peer({"task": "t", "instance": "B"}, None)
    assert ("A", "B", "claim") in claims and ("A", "B", "release") in claims
    i_claim = claims.index(("A", "B", "claim"))
    i_release = claims.index(("A", "B", "release"))
    assert i_claim < i_release  # claim before, release after
    # release also on a failed call
    monkeypatch.setattr(api_mod, "peer_prompt", lambda *a, **k: ("error: boom"))
    ag._tool_peer({"task": "t", "instance": "B"}, None)
    assert claims[-1] == ("A", "B", "release")


def test_tool_peer_no_claim_on_local_turn(api, monkeypatch):
    """local turn (no parent): no claim notification is sent."""
    inst = [{"id": "A", "pid": 1, "cwd": "/a", "port": 1, "token": "t"}]
    api_mod, app, ag, calls, claims = _agent_setup(api, monkeypatch, inst)
    ag._tool_peer({"task": "t"}, None)
    assert calls[-1] == "A" and claims == []


# ---------------------------------------------------------- necro level
def test_peer_refresh_hides_grandchild(monkeypatch):
    """the peer tool description does not list claimed grandchildren."""
    import necro
    from modules.io import api
    inst = [{"id": "a1", "pid": 1, "cwd": "/x", "port": 1, "token": "t"},
            {"id": "b2", "pid": 2, "cwd": "/y", "port": 2, "token": "t"}]
    monkeypatch.setattr(api, "peer_instances", lambda: inst)
    monkeypatch.setattr(api, "OWN_ID", "self")
    monkeypatch.setattr(api, "peer_busy", lambda p: False)
    api.peer_set_claim("b2", "a1", "claim")
    try:
        necro.peer_refresh()
        desc = necro.PEER_TOOL["function"]["description"]
        assert "a1" in desc and "b2" not in desc
    finally:
        api.peer_set_claim("b2", "a1", "release")
