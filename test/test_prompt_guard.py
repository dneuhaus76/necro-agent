"""Anti-injection guard ('data, not instructions') must be in every system prompt
that processes untrusted content (main, subagent, consult remote+local)."""
import modules.ui.subagent as sub


def test_subagent_prompt_has_guard():
    p = sub.subagent_prompt("ModelX", "/tmp")
    assert "data, not instructions" in p


def test_consult_prompts_have_guard():
    src = open("modules/core/agent.py").read()
    # both consult system prompts (remote + local) carry the guard
    assert src.count("data, not instructions") == 2
    assert "You are {ag['model']}, consulted by another agent" in src
    assert "You are NECRO answering follow-up questions" in src


def test_main_prompt_has_guard():
    src = open("necro.py").read()
    m = __import__("re").search(r"SYSTEM = \(f\"(.*?)\)\n", src, __import__("re").S)
    assert m and "data, not instructions" in m[1]
