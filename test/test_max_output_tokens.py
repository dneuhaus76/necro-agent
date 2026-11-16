""""think less" per request - when reasoning is OFF and max_output_tokens
is set (>0), the request body gets a max_tokens cap (best effort). Reasoning ON
or cap 0 -> no max_tokens. An explicit max_tokens in the body is never
overwritten (setdefault)."""
import threading

from modules.core.agent import Agent


def make_agent(reasoning, cap):
    app = type("A", (), {"stop": threading.Event(), "model": "m",
                         "reasoning": reasoning, "conns": []})()
    ag = Agent(app, base="http://local", step_timeout=10, tools_list=[], sub_tools=[],
               swarm=[], swarm_up={}, agents_md="", agents_md_path="",
               max_output_tokens=cap)
    ag.captured = {}

    def fake_open(base, body, conns, timeout=None, token=None):
        ag.captured.update(body)
        raise RuntimeError("stop")  # only the body matters

    import modules.core.inference as inf
    ag._orig_open = inf.open_conn
    inf.open_conn = fake_open
    return ag, app


def _request(ag):
    try:
        ag.request({"messages": []})
    except RuntimeError:
        pass
    finally:
        import modules.core.inference as inf
        inf.open_conn = ag._orig_open


def test_reasoning_off_cap_set():
    ag, _ = make_agent(False, 2048)
    _request(ag)
    assert ag.captured["max_tokens"] == 2048
    assert ag.captured["thinking"] is False


def test_reasoning_on_no_cap():
    ag, _ = make_agent(True, 2048)
    _request(ag)
    assert "max_tokens" not in ag.captured


def test_cap_zero_no_cap():
    ag, _ = make_agent(False, 0)
    _request(ag)
    assert "max_tokens" not in ag.captured


def test_explicit_max_tokens_not_overwritten():
    ag, _ = make_agent(False, 2048)
    try:
        ag.request({"messages": [], "max_tokens": 16})
    except RuntimeError:
        pass
    finally:
        import modules.core.inference as inf
        inf.open_conn = ag._orig_open
    assert ag.captured["max_tokens"] == 16
