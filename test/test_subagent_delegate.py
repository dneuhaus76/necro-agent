"""delegate inside sub-agents (toggle + depth cap). Default OFF keeps the
old SUB_TOOLS lock; when ON, a sub-agent at a depth below the cap gets
'delegate' re-added, at/above the cap it does not. Nested calls are counted in
_deleg_sub (delegate_stats visibility)."""
import threading

from modules.core.agent import Agent
from modules.core.context import delegate_stats

TOOLS = [{"function": {"name": n}} for n in
         ("bash", "read_file", "delegate", "consult", "peer")]
SUB_TOOLS = [t for t in TOOLS if t["function"]["name"] not in ("delegate", "consult", "peer")]


def make_agent(delegate=False, max_depth=1):
    app = type("A", (), {"stop": threading.Event(), "model": "m"})()
    ag = Agent(app, base="http://local", step_timeout=10, tools_list=TOOLS,
                 sub_tools=SUB_TOOLS, swarm=[{"url": "http://r", "model": "m"}],
                 swarm_up={("http://r", "m"): True}, agents_md="",
                 agents_md_path="", subagent_delegate=delegate,
                 subagent_max_depth=max_depth)
    ag.delegate = lambda ag_, task, blk=None, depth=1: "delegated"  # no real sub-loop
    return ag


def test_default_off_sub_tools_unchanged():
    ag = make_agent()
    names = {t["function"]["name"] for t in ag.subagent_tools(1)}
    assert "delegate" not in names
    assert names == {t["function"]["name"] for t in SUB_TOOLS}


def test_on_depth_below_cap_gets_delegate():
    ag = make_agent(delegate=True, max_depth=2)
    names = {t["function"]["name"] for t in ag.subagent_tools(1)}
    assert "delegate" in names
    assert "consult" not in names  # only delegate is re-added, never consult


def test_on_depth_at_cap_no_delegate():
    ag = make_agent(delegate=True, max_depth=1)
    names = {t["function"]["name"] for t in ag.subagent_tools(1)}
    assert "delegate" not in names  # cap 1 = direct delegate only, no nesting


def test_depth_counter_and_sub_count():
    ag = make_agent(delegate=True, max_depth=3)
    assert ag._cur_depth == 0
    # main agent calls delegate: child gets depth 1, not counted as nested
    ag._tool_delegate({"model": "m", "task": "t"}, blk=None)
    assert ag._deleg_sub == 0
    # inside a sub-agent loop (depth 1) a delegate call IS nested
    ag._cur_depth = 1
    ag._tool_delegate({"model": "m", "task": "t"}, blk=None)
    assert ag._deleg_sub == 1
    assert ag._cur_depth == 1  # unchanged (mocked delegate does not run the loop)


def test_reset_counters_clears_sub():
    ag = make_agent()
    ag._deleg_sub = 5
    ag.reset_counters()
    assert ag._deleg_sub == 0


def test_delegate_stats_includes_sub_delegates():
    items = [("delegate", "ok", 100), ("bash", "ok", 10)]
    d = delegate_stats(items, fallbacks=0, subagent_steps=3, sub_delegates=2)
    assert d == {"calls": 1, "ms": 100, "fallbacks": 0,
                 "subagent_steps": 3, "sub_delegates": 2}
