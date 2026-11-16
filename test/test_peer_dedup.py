"""the model sometimes emits the same peer call twice in one step
(parallel tool calls, identical instruction). run_calls fires both in parallel
-> two POSTs to the same peer. Dedup: an identical in-flight call (same target
+ same task) is waited on, not re-fired."""
import threading

import pytest


@pytest.fixture(autouse=True)
def _clean_inflight():
    from modules.core import agent as agent_mod
    yield
    agent_mod._PEER_INFLIGHT.clear()


def _mk_agent(monkeypatch, inst):
    from modules.io import api as api_mod
    from modules.core import agent as agent_mod

    class FakeApp:
        lock = threading.RLock()
        busy = False
        messages = []
        stop = threading.Event()

        def add(self, kind, text="", color=None):
            self.messages.append({"role": kind, "content": text})
            return None

        def set_head(self, b, head):
            pass

    monkeypatch.setattr(api_mod, "peer_instances", lambda: inst)
    monkeypatch.setattr(api_mod, "OWN_ID", "me")
    monkeypatch.setattr(api_mod, "peer_health", lambda x, timeout=2: (False, None))
    app = FakeApp()
    return agent_mod.Agent(app, "http://x/v1", 5, [], [], [], {}, "", ""), api_mod


def test_dedup_identical_inflight_peer_call(monkeypatch):
    """two identical peer calls in one step -> only ONE actual prompt fires;
    the second call waits and gets the same result."""
    ag, api_mod = _mk_agent(monkeypatch, [{"id": "B", "pid": 2, "cwd": "/b", "port": 2, "token": "t"}])
    calls = []
    gate = threading.Event()

    def fake_prompt(inst, task, timeout=None, hops=1, budget_left=None):
        calls.append(inst["id"])
        gate.wait(5)  # hold the first call in flight
        return "done"

    monkeypatch.setattr(api_mod, "peer_prompt", fake_prompt)
    ts = []

    def call():
        ts.append(ag._tool_peer({"task": "t"}, None))

    t1 = threading.Thread(target=call)
    t1.start()
    import time
    time.sleep(0.2)  # first call is now in flight
    t2 = threading.Thread(target=call)
    t2.start()
    gate.set()
    t1.join(5)
    t2.join(5)
    assert len(calls) == 1  # the duplicate was NOT re-fired
    assert ts == ["done", "done"]  # both got the result



def test_dedup_different_task_not_deduped(monkeypatch):
    """same peer, different task -> two calls (the dedup key includes the task)."""
    ag, api_mod = _mk_agent(monkeypatch, [{"id": "B", "pid": 2, "cwd": "/b", "port": 2, "token": "t"}])
    calls = []

    def fake_prompt(inst, task, timeout=None, hops=1, budget_left=None):
        calls.append(task)
        return "done"

    monkeypatch.setattr(api_mod, "peer_prompt", fake_prompt)
    assert ag._tool_peer({"task": "t1"}, None) == "done"
    assert ag._tool_peer({"task": "t2"}, None) == "done"
    # _tool_peer prefixes the task (role hint) - the raw task is the suffix
    assert len(calls) == 2 and calls[0] != calls[1]
    assert calls[0].endswith("t1") and calls[1].endswith("t2")


def test_dedup_finished_call_fires_again(monkeypatch):
    """after the first call finished, an identical call fires again (no stale
    result reuse - the slot is released and re-acquired)."""
    ag, api_mod = _mk_agent(monkeypatch, [{"id": "B", "pid": 2, "cwd": "/b", "port": 2, "token": "t"}])
    calls = []

    def fake_prompt(inst, task, timeout=None, hops=1, budget_left=None):
        calls.append(1)
        return "done"

    monkeypatch.setattr(api_mod, "peer_prompt", fake_prompt)
    assert ag._tool_peer({"task": "t"}, None) == "done"
    assert ag._tool_peer({"task": "t"}, None) == "done"
    assert len(calls) == 2


def test_dedup_error_propagates_to_waiter(monkeypatch):
    """if the first call raises, the waiting duplicate gets the same error
    (run_tool turns it into an error string for the model)."""
    ag, api_mod = _mk_agent(monkeypatch, [{"id": "B", "pid": 2, "cwd": "/b", "port": 2, "token": "t"}])
    gate = threading.Event()

    def fake_prompt(inst, task, timeout=None, hops=1, budget_left=None):
        gate.wait(5)
        raise RuntimeError("boom")

    monkeypatch.setattr(api_mod, "peer_prompt", fake_prompt)
    ts = []

    def call():
        try:
            ts.append(ag._tool_peer({"task": "t"}, None))
        except RuntimeError as e:  # run_tool would catch this into an error string
            ts.append(f"error: {e}")

    t1 = threading.Thread(target=call)
    t1.start()
    import time
    time.sleep(0.2)
    t2 = threading.Thread(target=call)
    t2.start()
    gate.set()
    t1.join(5)
    t2.join(5)
    # both calls saw the error (the waiter re-raised the first call's exception)
    assert ts == ["error: boom", "error: boom"]
