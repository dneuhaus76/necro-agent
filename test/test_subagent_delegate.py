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
    # consult rides on the same toggle: a stuck sub-agent gets an opinion
    # instead of another work loop (no tools, bounded 4 rounds)
    assert "consult" in names
    assert "peer" not in names  # peering stays a main/peer-turn feature


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


def test_self_target_is_rejected():
    """A sub-agent running on (url, model) must not consult/delegate into that
    same endpoint - same model, less context, pure noise."""
    ag = make_agent(delegate=True, max_depth=2)
    ag._cur_ep = ("http://r", "m")  # this loop runs on the swarm agent
    err = ag._self_target_err({"url": "http://r", "model": "m"}, "consult")
    assert err and err.startswith("error: consult")
    assert ag._self_target_err({"url": "http://other", "model": "m"}, "consult") is None
    ag._cur_ep = None  # main agent: no restriction
    assert ag._self_target_err({"url": "http://r", "model": "m"}, "delegate") is None


def test_peer_hop_cap_enforced_at_dispatch(monkeypatch):
    """peer_turn_tools() hides 'peer' beyond the hop cap, but dispatch does not
    check the request's tool list - the cap must also refuse the call.
    The sibling is faked: a real one (left over from another test, or a live
    instance) made this test pass by accident - it failed as soon as the leaks
    were closed."""
    from modules.io import api
    monkeypatch.setattr(api, "peer_instances", lambda: [{"id": "sib", "host": "127.0.0.1",
                                                         "port": 1, "token": "t"}])
    monkeypatch.setattr(api, "peer_excluded_ids", lambda: set())
    ag = make_agent()
    ag.peer_max_hops = 1
    ag.app._peer_hops = 1  # this turn IS a peer turn: the call would be hop 2
    err = ag._tool_peer({"task": "t"}, None)
    assert err.startswith("error: peer chain limit reached")


# ---- worker prompt: rules, tools, project document, skills -----------------
def _worker_prompt_agent(delegate=False, max_depth=2):
    ag = make_agent(delegate=delegate, max_depth=max_depth)
    ag.agents_md, ag.agents_md_path = "PROJECT-RULES", "/p/NECRO.md"
    return ag


def test_worker_prompt_names_the_real_toolset():
    """the prompt must not advertise a tool the request does not carry (and must
    not hide one it does: look was missing since it was added)."""
    from modules.ui.subagent import subagent_prompt
    tools = [{"function": {"name": n}} for n in ("bash", "grep", "look")]
    p = subagent_prompt("M", "/w", tools=tools)
    assert "Tools: bash, grep, look." in p
    assert "delegate" not in p


def test_worker_prompt_carries_the_shared_rules():
    from modules.ui.subagent import RULES
    from modules.core.agent import Agent
    ag = _worker_prompt_agent()
    p = ag._worker_prompt("M", ag.subagent_tools(1))
    assert RULES in p  # grep-not-cat, oldText, bias for action, guard
    assert "Today:" in p  # the worker knows the date, like the main agent
    assert "PROJECT-RULES" in p  # same project document as the caller


def test_worker_prompt_has_skills_hint():
    """the skills index lives in the agent home; a rooted join dropped it and the
    hint never appeared."""
    import os
    from modules.ui.subagent import SKILLS_INDEX
    assert os.path.exists(SKILLS_INDEX), SKILLS_INDEX  # the fixed path must resolve
    from modules.ui.subagent import subagent_prompt
    p = subagent_prompt("M", "/w", skills_index="/home/appuser/agent/skills/INDEX.md")
    assert "Skills: /home/appuser/agent/skills/INDEX.md" in p
    # a missing index stays silent (lean prompt for projects without skills)
    assert "Skills:" not in subagent_prompt("M", "/w", skills_index="/nope/INDEX.md")
