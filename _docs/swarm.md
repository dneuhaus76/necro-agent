# NECRO – Swarm (delegate / consult)

A swarm is a list of other models (each on its own inference server) that the
main model can use as sub-agents. Pure logic lives in `modules/io/swarm.py`
(parameterized, unit-testable); the tool loops live in
`modules/core/agent.py` (`delegate()`, `consult()`, `_agent_loop()`).

## Configuration

| Source | Form |
|---|---|
| `necro.cfg` key `swarm` | list `[{url?, model}]` (empty url = main base) |
| env `NECRO_SWARM` | `model=url;model=url` (always wins over cfg; empty = explicit off) |

**Master switches:** the swarm is OFF by default. It is active via
`NECRO_SWARM` (explicit env list) or `remote=true` / `NECRO_REMOTE` (cfg
list). `PEERING`/`peering` does NOT activate the cfg swarm list (it only
enables the Peer-API — see peer-mechanism.md).

The **main model is filtered out** of the list: delegating to itself (same
url + model) would be a self-call.

## Name resolution (`swarm.find`)

1. `name@host` — explicit disambiguation for duplicate model names on
   different servers.
2. Exact model-name match; ambiguous (2+ servers) → error demanding
   `name@host`.
3. Fuzzy fallback: closest by `difflib.SequenceMatcher` ratio; a ratio > 0.6
   adds a "did you mean …?" hint.

Tool descriptions list all agents as `name (url)`; duplicates as
`name@host (url)`.

## Availability check (background)

`check_loop` probes every agent with `GET /models` (3 s timeout) every 30 s
(`NECRO_SWARM_CHECK`). Each agent gets up to 2 attempts (a single transient
hiccup must not flip the status for the whole cycle). `on_change` fires only
when a status actually flips — the header shows on/off per agent, so an
offline swarm is visible before a delegate call fails.

`_tool_delegate`/`_tool_consult` do a pre-flight check against this map: an
agent marked offline fails fast with a precise error instead of burning a
request on a dead host.

## delegate

- Runs its own tool loop (`_agent_loop`, **max 10 steps**) on the swarm
  model: own context (system prompt + task), same working directory, tools =
  `SUB_TOOLS` (see below).
- **Live progress:** the tool block header shows step + elapsed time
  (`progress_head`) — a delegate can take minutes, the user must see the
  session is working. Each sub-step (tool call + result excerpt) is logged
  into the block.
- **Fallback:** if the swarm agent fails mid-run (network, server crash), the
  main agent takes over the task itself (`delegate_fallback`) — same loop,
  local model, **same nesting depth** (so the depth cap is enforced on the
  fallback path too). A user abort (`stop` set) is NOT a swarm failure: no
  fallback, the turn ends.
- Fallbacks are counted per turn and land in the trace
  (`delegates.fallbacks`) — a quality signal for swarm reliability.

### Nested delegate (opt-in)

`subagent_delegate` (default `false`) re-adds `delegate` to sub-agents for
parallel decomposition. Nesting is explicitly capped by `subagent_max_depth`
(default 1 = direct delegate only) — the cap, not the 10-step loop budget,
is the limit. `subagent_tools(depth)` builds the child's toolset from the
running loop's depth. Nested calls are counted in the trace
(`delegates.sub_delegates`).

## consult

Multi-round bot-to-bot dialogue (no tools on the remote side):

- The remote model may ask follow-up questions prefixed `QUESTION:` (one at
  a time, **max 3**); the final answer is prefixed `ANSWER:`.
- The local agent answers with its **full conversation context** (a local
  completion without tools).
- Old tool results are trimmed to 2000 chars (`CONSULT_TRIM`) both when the
  local context is built and when each round is appended — the remote
  context stays bounded across rounds.
- The dangling assistant `tool_calls` pair (consult runs inside `run_calls`,
  before the tool results are in the context) is dropped — strict servers
  reject "assistant tool_calls not followed by tool messages".
- An **empty remote reply is an error** (`ReplyError`), not a silent empty
  answer.
- Rounds: initial answer + up to 3 question rounds (max 4 completions).

## Ping-pong protection

Sub-agents never receive the `delegate`/`consult`/`peer` tools
(`SUB_TOOLS`), so a sibling cannot hand tasks back to the master while it is
busy. This is a tool-level guard, not a hard guarantee — the model can still
be prompted to do such things.

Peer turns are different: they use `peer_turn_tools(hops)` (see
peer-mechanism.md), which keeps `delegate`/`consult` when
`PEER_TURN_SUBAGENTS` is on (default) and re-adds `peer` while
`hops < PEER_MAX_HOPS`.

## Worker scaling

`run_calls` parallelism scales with the swarm: 4 base workers + 1 per
currently-online swarm agent, capped at 8 (`swarm_workers`). More (good)
models online → more parallel tool calls.

## Trace

Per turn, `delegate_stats()` writes `delegates: {calls, ms, fallbacks,
subagent_steps, sub_delegates}` into the trace row (see data-model.md).
