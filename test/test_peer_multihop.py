"""multi-hop peering - a peer turn may call another peer (hop limit 2),
no bounce back to the caller (frm filter), chain wall-clock budget, subagent
toggle, busy status in the peer list + idle-first routing."""
import json
import threading
import time
import urllib.request

import pytest


# ---------------------------------------------------------------- necro level
def _fake_tool(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


def test_peer_turn_tools_matrix(monkeypatch):
    """peer tool only while hops < PEER_MAX_HOPS; consult always; delegate only
    when PEER_TURN_SUBAGENTS is on. (TOOLS extended with the peer/swarm tools
    that REMOTE/SWARM would register at startup.)"""
    import necro
    monkeypatch.setattr(necro, "TOOLS", necro.TOOLS + [necro.PEER_TOOL, _fake_tool("delegate"), _fake_tool("consult")])

    def names(hops):
        return {t["function"]["name"] for t in necro.peer_turn_tools(hops)}

    monkeypatch.setattr(necro, "PEER_MAX_HOPS", 2)
    monkeypatch.setattr(necro, "PEER_TURN_SUBAGENTS", True)
    assert "peer" in names(1) and "delegate" in names(1) and "consult" in names(1)  # hop 1: full set
    assert "peer" not in names(2) and "delegate" in names(2)  # hop 2: limit reached, delegate stays
    monkeypatch.setattr(necro, "PEER_TURN_SUBAGENTS", False)
    assert "peer" in names(1) and "delegate" not in names(1)  # delegate off
    assert "consult" in names(1)  # consult always: bounded, no tools, cheap
    monkeypatch.setattr(necro, "PEER_MAX_HOPS", 1)
    assert "peer" not in names(1)  # limit 1 = old behaviour (no multi-hop)


def test_peer_turn_delegate_default_off(monkeypatch):
    """default: a peer turn gets no delegate (no third server while the caller
    blocks), but consult stays available."""
    import necro
    monkeypatch.undo()  # the module value as shipped, not a test override
    monkeypatch.setattr(necro, "TOOLS", necro.TOOLS + [necro.PEER_TOOL, _fake_tool("delegate"), _fake_tool("consult")])
    names = {t["function"]["name"] for t in necro.peer_turn_tools(1)}
    assert "delegate" not in names and "consult" in names


def test_peer_refresh_shows_busy_status(monkeypatch):
    """the peer tool description carries the (cached) busy status per instance."""
    import necro
    from modules.io import api
    inst = [{"id": "a1", "pid": 1, "cwd": "/x", "port": 1, "token": "t"},
            {"id": "b2", "pid": 2, "cwd": "/y", "port": 2, "token": "t"}]
    monkeypatch.setattr(api, "peer_instances", lambda: inst)
    monkeypatch.setattr(api, "OWN_ID", "self")
    monkeypatch.setattr(api, "peer_busy", lambda p: {"a1": True, "b2": False}[p["id"]])
    necro.peer_refresh()
    desc = necro.PEER_TOOL["function"]["description"]
    assert "a1" in desc and "busy" in desc
    assert "b2" in desc and "idle" in desc


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
        self.state_during_turn = {}

    def add(self, kind, text="", color=None):
        self.messages.append({"role": kind, "content": text})
        return None

    def db_sync(self):
        pass

    def turn(self, chat_id=None, done=None):
        self.state_during_turn = {  # captured while the worker's state is active
            "hops": getattr(self, "_peer_hops", None),
            "from": getattr(self, "_peer_from", None),
            "chain_t0": getattr(self, "_chain_t0", None),
            "chain_budget": getattr(self, "_chain_budget", None),
        }
        self._running.set()
        time.sleep(0.2)
        self._running.clear()
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


def _post(p, task, frm="other", hops=1, budget_left=None, timeout=10):
    body = {"task": task, "from": frm, "hops": hops}
    if budget_left is not None:
        body["budget_left"] = budget_left
    req = urllib.request.Request(
        f"http://127.0.0.1:{p.port}/api/prompt",
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "X-Necro-Token": p.token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def test_worker_receives_hops_and_state(api):
    """hops/budget from the POST body reach the worker and are stored on the app
    for the duration of the peer turn (the toolset builder + _tool_peer read them)."""
    api_mod, p, app = api
    status, body = _post(p, "task1", frm="A", hops=2, budget_left=500)
    assert status == 200
    assert body["result"] == "ok"
    s = app.state_during_turn
    assert s["hops"] == 2
    assert s["from"] == "A"
    assert s["chain_t0"] is not None  # chain clock started for this turn
    assert s["chain_budget"] == 500  # budget 500 < own timeout 600 -> budget wins
    assert app._peer_hops == 0 and app._peer_from is None  # reset after the turn


def test_budget_caps_watchdog(api):
    """a small chain budget aborts a hung peer turn after the budget, not after
    the full peer_turn_timeout_s (the whole chain shares one wall-clock budget)."""
    api_mod, p, app = api
    p.peer_turn_timeout = 30  # large own timeout - the budget must be what fires

    def hung_turn(chat_id=None, done=None):
        app.busy = True
        app._running.set()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5 and not app.stop.is_set():
            time.sleep(0.05)
        app.stop_seen = app.stop.is_set()
        app._running.clear()
        app.busy = False
        if done is not None:
            done.set()

    app.turn = hung_turn
    status, _ = _post(p, "task1", hops=1, budget_left=0.2, timeout=15)
    assert status == 200
    assert app.stop_seen  # aborted by the budget-capped watchdog
    assert app.busy is False


def test_peer_prompt_sends_hops_and_budget(api, monkeypatch):
    """peer_prompt puts hops/budget_left into the POST body (hops=1 default for
    a direct call from a local turn)."""
    api_mod, p, app = api
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["body"] = json.loads(req.data)
        raise ConnectionError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    inst = json.loads(p.path.read_text())
    api_mod.peer_prompt(inst, "task", hops=2, budget_left=42)
    assert captured["body"]["hops"] == 2
    assert captured["body"]["budget_left"] == 42
    assert captured["body"]["from"] == api_mod.OWN_ID


def test_peer_prompt_direct_call_omits_budget(api, monkeypatch):
    """a direct call (hops=1, no budget) sends hops=1 and no budget_left key -
    the receiver falls back to its own peer_turn_timeout_s."""
    api_mod, p, app = api
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["body"] = json.loads(req.data)
        raise ConnectionError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    inst = json.loads(p.path.read_text())
    api_mod.peer_prompt(inst, "task")
    assert captured["body"]["hops"] == 1
    assert "budget_left" not in captured["body"]


def test_peer_prompt_timeout_capped_by_budget(api, monkeypatch):
    """the caller's HTTP wait is capped by the remaining chain budget (+30 s),
    not the peer's full configured timeout."""
    api_mod, p, app = api
    d = json.loads(p.path.read_text())
    d["peer_turn_timeout_s"] = 1234
    p.path.write_text(json.dumps(d))
    captured = {}

    def fake_urlopen(req, timeout=5):
        captured["timeout"] = timeout
        raise ConnectionError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    inst = json.loads(p.path.read_text())
    api_mod.peer_prompt(inst, "task", hops=2, budget_left=10)
    assert captured["timeout"] == 10 + 30  # budget wins over the peer's 1234 s


# ------------------------------------------------------------- agent level
def test_tool_peer_filters_frm_and_routes_idle(api, monkeypatch):
    """in a peer turn (frm set) the sender is excluded from the candidate list;
    without an explicit target an idle peer is preferred (cached status)."""
    api_mod, p, app = api
    from modules.core import agent as agent_mod
    inst = [{"id": "A", "pid": 1, "cwd": "/a", "port": 1, "token": "t"},
            {"id": "B", "pid": 2, "cwd": "/b", "port": 2, "token": "t"},
            {"id": "C", "pid": 3, "cwd": "/c", "port": 3, "token": "t"}]
    monkeypatch.setattr(api_mod, "peer_instances", lambda: inst)
    monkeypatch.setattr(api_mod, "OWN_ID", "me")
    monkeypatch.setattr(api_mod, "peer_busy", lambda x: {"B": True, "C": False}.get(x["id"], True))
    calls = []

    def fake_prompt(inst, task, timeout=None, hops=1, budget_left=None):
        calls.append((inst["id"], hops, budget_left))
        return "done"

    monkeypatch.setattr(api_mod, "peer_prompt", fake_prompt)
    monkeypatch.setattr(api_mod, "peer_health", lambda x, timeout=2: (False, None))
    # peer_max_hops=2: this test IS the multi-hop case (a peer turn calls a peer)
    ag = agent_mod.Agent(app, "http://x/v1", 5, [], [], [], {}, "", "", peer_max_hops=2)
    # local turn: no frm filter, B busy -> idle C is picked
    assert ag._tool_peer({"task": "t"}, None) == "done"
    assert calls[-1][0] == "C" and calls[-1][1] == 1 and calls[-1][2] is None
    # peer turn from A: A excluded, B busy -> C again; explicit target wins
    app._peer_from, app._peer_hops = "A", 1
    app._chain_t0 = time.monotonic() - 10
    app._chain_budget = 60
    ag._tool_peer({"task": "t"}, None)
    assert calls[-1][0] == "C" and calls[-1][1] == 2
    assert calls[-1][2] is not None and 45 <= calls[-1][2] <= 50  # remaining chain budget
    ag._tool_peer({"task": "t", "instance": "B"}, None)
    assert calls[-1][0] == "B"  # explicit target beats idle routing


def test_tool_peer_no_forward_peer(api, monkeypatch):
    """peer turn with only the sender as sibling -> no forward peer (no bounce)."""
    api_mod, p, app = api
    from modules.core import agent as agent_mod
    monkeypatch.setattr(api_mod, "peer_instances",
                        lambda: [{"id": "A", "pid": 1, "cwd": "/a", "port": 1, "token": "t"}])
    monkeypatch.setattr(api_mod, "OWN_ID", "me")
    ag = agent_mod.Agent(app, "http://x/v1", 5, [], [], [], {}, "", "")
    app._peer_from, app._peer_hops = "A", 1
    r = ag._tool_peer({"task": "t"}, None)
    assert r.startswith("error: no sibling NECRO instance found")
