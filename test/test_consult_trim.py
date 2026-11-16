"""the consult remote context is trimmed per round - long remote answers and
local answers are cut to CONSULT_TRIM before being appended, so the context does
not grow linearly with the number of rounds. The returned final answer stays full."""
import threading

from modules.core.agent import Agent, CONSULT_TRIM


class FakeApp:
    def __init__(self):
        self.messages = [{"role": "system", "content": "sys"},
                         {"role": "user", "content": "hi"}]
        self.stop = threading.Event()


def make_agent(remote_script):
    app = FakeApp()
    ag = Agent(app, base="http://local", step_timeout=10, tools_list=[], sub_tools=[],
               swarm=[], swarm_up={}, agents_md="", agents_md_path="")
    ag.remote_calls = []

    def complete(msgs, base=None, model=None):
        if base is not None:  # remote call
            ag.remote_calls.append(msgs)
            return remote_script.pop(0)
        return "L" * 5000  # long local answer

    ag.complete = complete
    return ag, app


def test_remote_context_trimmed_per_round():
    long_q = "QUESTION: q2 " + "y" * 5000
    ag, _ = make_agent([long_q, "ANSWER: final"])
    out = ag.consult({"model": "m", "url": "http://r"}, "question")
    assert out == "ANSWER: final"  # returned answer is not trimmed
    # second remote call carries the previous round - both messages trimmed
    prev = [m for m in ag.remote_calls[1] if m["role"] in ("assistant", "user")][1:]
    assert all(len(m["content"]) <= CONSULT_TRIM for m in prev)
    assert any("y" * 5000 not in m["content"] for m in ag.remote_calls[1])


def test_short_messages_untouched():
    ag, _ = make_agent(["QUESTION: q1", "ANSWER: ok"])
    out = ag.consult({"model": "m", "url": "http://r"}, "question")
    assert out == "ANSWER: ok"
    second = ag.remote_calls[1]
    assert {"role": "assistant", "content": "QUESTION: q1"} in second
    assert {"role": "user", "content": "L" * 5000} not in second  # local answer trimmed
