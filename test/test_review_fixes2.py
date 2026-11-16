"""Regression tests for the 2026-07 review fixes:
- dedup stale-result race (generation counter, Condition-based)
- HTTP 4xx non-retryable (inference.HTTPError re-raised by _retry)
- delegate fallback depth cap (fallback passes its depth explicitly)"""
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _clean_inflight():
    from modules.core import agent as agent_mod
    yield
    agent_mod._PEER_INFLIGHT.clear()


def test_dedup_stale_result_race():
    """The reported race: A runs and finishes (slot holds a stale result A).
    B starts and is in flight. C arrives while B runs: with the old code C saw
    A's stale result and returned it. Now C must WAIT for B and get B's result.
    Deterministic: B is held in flight by gate_release; C parks the moment it
    sees running=True (a 0.1 s sleep guarantees C is parked before B is
    released - no reliance on timeout)."""
    from modules.core import agent as agent_mod

    assert agent_mod._peer_call_dedup("k", lambda: "A") == ("A", True)  # stale result A in slot

    gate_b = threading.Event()      # B is in flight
    gate_release = threading.Event()  # release B to finish
    r_b, r_c = [None], [None]
    b_done, c_done = threading.Event(), threading.Event()

    def t_b():
        r_b[0] = agent_mod._peer_call_dedup("k",
                                            lambda: (gate_b.set(), gate_release.wait(5), "B")[2])
        b_done.set()

    def t_c():
        gate_b.wait(5)  # B is in flight - the race window
        r_c[0] = agent_mod._peer_call_dedup("k", lambda: "C-should-not-run")
        c_done.set()

    tb, tc = threading.Thread(target=t_b), threading.Thread(target=t_c)
    tb.start()
    tc.start()
    gate_b.wait(5)   # B is in flight
    time.sleep(0.1)  # C has parked inside dedup (it saw running=True)
    gate_release.set()  # let B finish with "B"
    b_done.wait(5)
    c_done.wait(5)
    assert r_b[0] == ("B", True)
    assert r_c[0] == ("B", False), f"C got {r_c[0]!r} - stale result race not fixed"


def test_dedup_waiter_gets_fresh_run_not_stale():
    """After a finished run, a NEW run with a different result: a concurrent
    waiter must get the NEW result, never the old one."""
    from modules.core import agent as agent_mod

    assert agent_mod._peer_call_dedup("k", lambda: "old") == ("old", True)

    gate = threading.Event()
    res = [None]

    def t_waiter():
        gate.wait(5)  # arrive while the second run is in flight
        res[0] = agent_mod._peer_call_dedup("k", lambda: "never")

    tw = threading.Thread(target=t_waiter)
    tw.start()
    # the runner must be in flight when the gate opens: set the gate FIRST, then
    # keep running - the waiter proceeds while running=True and must get the
    # runner's result (not acquire the slot itself after the runner finished)
    r2 = agent_mod._peer_call_dedup("k", lambda: (gate.set(), time.sleep(0.2), "new")[2])
    tw.join(5)
    assert r2 == ("new", True)  # runner: its own result, fired
    assert res[0] == ("new", False)  # waiter: the runner's result, not its own


def test_retry_http_4xx_not_retried():
    """A non-200 (inference.HTTPError) must be re-raised immediately - no
    backoff attempts (a 400/401 is a definitive rejection)."""
    from modules.core import agent as agent_mod
    from modules.core import inference

    class FakeApp:
        stop = threading.Event()
        def add(self, kind, text="", color=None):
            pass

    ag = agent_mod.Agent(FakeApp(), "http://x/v1", 5, [], [], [], {}, "", "")
    attempts = []

    def fn():
        attempts.append(1)
        raise inference.HTTPError("HTTP 400: bad request")

    t0 = time.monotonic()
    with pytest.raises(inference.HTTPError):
        ag._retry(fn, "test")
    assert len(attempts) == 1, f"HTTP 4xx retried {len(attempts)}x - must fail fast"
    assert time.monotonic() - t0 < 1.0  # no backoff sleep


def test_retry_connect_error_still_retries(monkeypatch):
    """Connection errors (server still loading) must still be retried."""
    from modules.core import agent as agent_mod

    # shrink the backoff: the test verifies the RETRY COUNT, not the wait curve
    monkeypatch.setattr(agent_mod, "RETRY_MAX_WAIT", 0.01)

    class FakeApp:
        stop = threading.Event()
        def add(self, kind, text="", color=None):
            pass

    ag = agent_mod.Agent(FakeApp(), "http://x/v1", 5, [], [], [], {}, "", "")
    attempts = []

    def fn():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionRefusedError("conn refused")
        return "ok"

    assert ag._retry(fn, "test") == "ok"
    assert len(attempts) == 3


def test_fallback_depth_cap_enforced():
    """delegate_fallback must build its toolset for the DELEGATE's depth, not
    the parent's. With subagent_max_depth=1 a fallback at depth 1 must NOT
    contain 'delegate' (the cap), otherwise a nested delegate inside the
    fallback could re-fire at depth+1 and bypass the cap."""
    from modules.core import agent as agent_mod

    class FakeApp:
        stop = threading.Event()
        messages = []
        model = "m"
        def add(self, kind, text="", color=None):
            pass

    app = FakeApp()
    # tools_list must contain a delegate tool: subagent_tools() filters the MAIN
    # toolset by name, an empty list would make the cap check unobservable
    delegate_tool = {"function": {"name": "delegate"}}
    ag = agent_mod.Agent(app, "http://x/v1", 5, [delegate_tool], [], [], {}, "", "",
                         subagent_delegate=True, subagent_max_depth=1)
    seen = []

    def fake_loop(sysp, task, base, model, log, blk=None, tools=None):
        seen.append({t["function"]["name"] for t in tools})
        return "fallback done"

    ag._agent_loop = fake_loop
    res = ag.delegate_fallback({"url": "http://swarm/v1", "model": "m"}, "task",
                               RuntimeError("swarm down"), None, depth=1)
    assert res == "fallback done"
    assert seen, "fallback loop did not run"
    assert "delegate" not in seen[0], \
        f"fallback toolset re-added delegate at cap: {seen[0]}"

    # and below the cap the fallback DOES get delegate (depth 1 < max 2)
    ag2 = agent_mod.Agent(FakeApp(), "http://x/v1", 5, [delegate_tool], [], [], {}, "", "",
                          subagent_delegate=True, subagent_max_depth=2)
    seen2 = []
    ag2._agent_loop = lambda sysp, task, base, model, log, blk=None, tools=None: (
        seen2.append({t["function"]["name"] for t in tools}), "ok")[1]
    ag2.delegate_fallback({"url": "u", "model": "m"}, "task", RuntimeError("x"), None, depth=1)
    assert "delegate" in seen2[0]


def test_delegate_passes_depth_to_fallback():
    """The real bug path: delegate() -> swarm loop fails -> fallback. The
    fallback must run with the DELEGATE's depth toolset. With max_depth=2 the
    delegate at depth 1 gets 'delegate' in its toolset; if the fallback used
    the parent's depth (0) it would ALSO get delegate - indistinguishable.
    Distinguish via max_depth=1: delegate at depth 1 has NO delegate; a buggy
    fallback at depth 0 WOULD have it."""
    from modules.core import agent as agent_mod

    class FakeApp:
        stop = threading.Event()
        messages = []
        model = "m"
        def add(self, kind, text="", color=None):
            pass

    app = FakeApp()
    ag = agent_mod.Agent(app, "http://x/v1", 5, [], [], [], {}, "", "",
                         subagent_delegate=True, subagent_max_depth=1)
    seen = []

    def fake_loop(sysp, task, base, model, log, blk=None, tools=None):
        names = {t["function"]["name"] for t in tools}
        seen.append(names)
        if base == "http://swarm/v1":  # the swarm attempt
            raise ConnectionError("swarm down")
        return "fallback done"

    ag._agent_loop = fake_loop
    res = ag.delegate({"url": "http://swarm/v1", "model": "m"}, "task", None, depth=1)
    assert res == "fallback done"
    assert len(seen) == 2, f"expected swarm attempt + fallback, got {len(seen)}"
    assert "delegate" not in seen[1], \
        f"fallback toolset has delegate at cap (depth bypass): {seen[1]}"
