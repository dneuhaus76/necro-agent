"""delegate/consult pre-flight: a fresh 'up' from the background check is
trusted; any other status (False, None) triggers a live TCP port check.
Port closed -> fail fast, no request burned on a dead host."""
import threading

import modules.core.agent as agent_mod
from modules.core.agent import Agent


class FakeApp:
    def __init__(self):
        self.messages = [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "hi"}]
        self.stop = threading.Event()
        self.model = "local-model"


SWARM = [{"url": "http://r", "model": "m"}]


def make_agent(up):
    app = FakeApp()
    ag = Agent(app, base="http://local", step_timeout=10, tools_list=[], sub_tools=[],
               swarm=SWARM, swarm_up=up, agents_md="", agents_md_path="")
    return ag


def with_port(ag, open_):
    """patch the live port check (no real network in tests)"""
    agent_mod.port_open = lambda url, timeout=3: open_
    return ag


def test_delegate_offline_fails_fast():
    ag = with_port(make_agent({("http://r", "m"): False}), False)
    ag.delegate = lambda *a, **k: "should not be called"
    out = ag._tool_delegate({"model": "m", "task": "t"}, None)
    assert out.startswith("error: swarm agent 'm' is offline")


def test_consult_offline_fails_fast():
    ag = with_port(make_agent({("http://r", "m"): False}), False)
    ag.consult = lambda *a, **k: "should not be called"
    out = ag._tool_consult({"model": "m", "question": "q"}, None)
    assert out.startswith("error: swarm agent 'm' is offline")


def test_unknown_status_port_open_passes_through():
    # check not run yet (None): live port check decides
    ag = with_port(make_agent({}), True)
    ag.delegate = lambda *a, **k: "delegated"
    ag.consult = lambda *a, **k: "consulted"
    assert ag._tool_delegate({"model": "m", "task": "t"}, None) == "delegated"
    assert ag._tool_consult({"model": "m", "question": "q"}, None) == "consulted"


def test_unknown_status_port_closed_fails_fast():
    ag = with_port(make_agent({}), False)
    ag.delegate = lambda *a, **k: "should not be called"
    out = ag._tool_delegate({"model": "m", "task": "t"}, None)
    assert out.startswith("error: swarm agent 'm' is offline")


def test_online_status_skips_port_check():
    # fresh 'up' from the background check: no port check needed
    ag = make_agent({("http://r", "m"): True})
    agent_mod.port_open = lambda url, timeout=3: (_ for _ in ()).throw(AssertionError("no port check"))
    ag.delegate = lambda *a, **k: "delegated"
    ag.consult = lambda *a, **k: "consulted"
    assert ag._tool_delegate({"model": "m", "task": "t"}, None) == "delegated"
    assert ag._tool_consult({"model": "m", "question": "q"}, None) == "consulted"
