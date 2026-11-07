"""NECRO context management: pure helpers for keeping the context lean.
No App state - unit-testable without a server."""
def trim_tool_results(messages, max_len=4000, keep=2000):
    """
    @stable | verified: 2026-11 - frozen: change only on failing test or hard requirement
    Trim 'tool' results longer than max_len to keep chars + a size marker.
    Cheap (no LLM call) first pass for compact(): a single huge recent tool result
    (e.g. a giant read_file / bash dump) can keep the context above the threshold
    even after the head was summarised, so compact() would re-trigger every step.
    Trimming those frees context without another completion. Only 'tool' role
    messages are touched (a tool-call pair is never split - content shrinks in place);
    the marker preserves the total size so the model knows content was cut.
    Returns (new_messages, n_trimmed)."""
    out, n = [], 0
    for m in messages:
        c = m.get("content") or ""
        if m.get("role") == "tool" and len(c) > max_len:
            out.append(dict(m, content=c[:keep] + f"\u2026[trimmed, {len(c)} chars total]"))
            n += 1
        else:
            out.append(m)
    return out, n
def under_after_trim(used, est_before, est_after, ctx):
    """
    @stable | verified: 2026-11 - frozen: change only on failing test or hard requirement
    did trimming bring the context under the 80 % threshold? used = real
    prompt_tokens from the last API call (else None); est_before/est_after = char//3
    estimates before/after trimming. The estimate is a rough proxy (json chars // 3)
    and can sit well BELOW the real usage - deciding on it alone would skip the
    head-summary while the real context is still over the threshold (compact() then
    re-triggers every step = repeated LLM summaries). With real usage
    available, the freed chars are scaled by used/est_before and subtracted from
    used instead. No real usage yet -> plain estimate comparison (as before)."""
    if not used:
        return est_after < ctx * 0.8
    if est_before <= 0:
        return True
    return used - (est_before - est_after) * (used / est_before) < ctx * 0.8
def tool_stats(results):
    """measurable tool-call metrics for the turn trace: (fails, chars).
    fails = results starting with 'error:' (the harness error convention - the model
    sees exactly this prefix, so it is the right quality signal); chars = total result
    size (proxy for the context/token cost of tool output)."""
    fails = sum(1 for r in results if r.lstrip().startswith("error:"))
    return fails, sum(len(r) for r in results)
def delegate_stats(items, fallbacks=0, subagent_steps=0):
    """delegation metrics for the turn trace: {calls, ms, fallbacks, subagent_steps}.
    items = (name, result, ms) triples; calls = delegate/consult calls. fallbacks =
    takeovers (swarm agent died, main agent took over - the quality signal);
    subagent_steps = total sub-agent loop steps (work volume)."""
    d = [i for i in items if i[0] in ("delegate", "consult")]
    return {"calls": len(d), "ms": sum(ms for _, _, ms in d),
            "fallbacks": fallbacks, "subagent_steps": subagent_steps}
def tool_detail(items):
    """per-tool breakdown of a turn: {name: {calls, fails, chars, ms}}.
    items = (name, result, ms) triples; same 'error:' convention as tool_stats().
    Shows which tool fails most / is slowest -> the next targeted improvement."""
    d = {}
    for name, res, ms in items:
        e = d.setdefault(name, {"calls": 0, "fails": 0, "chars": 0, "ms": 0})
        e["calls"] += 1
        e["fails"] += 1 if res.lstrip().startswith("error:") else 0
        e["chars"] += len(res)
        e["ms"] += int(ms)
    return d
