# NECRO – Agent and Turn Lifecycle

The core of NECRO is the agent loop: one LLM turn that alternates between
inference (streaming) and tool execution until the model requests no more
tools or an abort condition triggers. The turn flow controller (`turn()`,
safety breaks, trace) lives in `modules/core/turn.py` (a `TurnMixin` on
`App`); the pure agent logic (`stream`, `compact`, tool execution) lives in
`modules/core/agent.py`. Neither touches the terminal directly, so the turn
stays headless-testable.

## Architecture

```
App.turn()  (modules/core/turn.py)      Agent (modules/core/agent.py)
      │                                     │
      │  for step in range(MAX_STEPS)       │
      ├── safety breaks (stop/timeout/turns)│
      ├── agent.compact() ─────────────────►│  context < 80 % of the window
      ├── agent.stream(tools) ─────────────►│  SSE stream -> text + tool calls
      │      │                              │
      │      ▼                              │
      ├── agent.run_calls(calls) ──────────►│  parallel tool execution
      │      │                              │
      │      └─ no calls? ─────────────────► turn ends (agent reply)
```

## The Turn (`TurnMixin.turn`, modules/core/turn.py)

A turn is a single user request. It runs in its own thread (`_start_turn`)
and repeats up to `MAX_STEPS` steps (default 25):

1. **Safety breaks** (checked per step):
   - `self.stop` (user abort, e.g. `^c` / `/stop`) → immediate abort.
   - `TURN_TIMEOUT` (wall clock, default 3600 s) → `TimeoutError`.
   - `MAX_TURNS` (per session, default 200) → turn ends, `/new` required.
2. **Queued prompt** — a prompt typed while the turn was running starts at
   the NEXT step boundary (before the agent decides its next action), not
   after the whole turn. The old turn breaks out; its `finally` must not
   clobber the new turn's state (guarded by the turn counter at entry).
3. **Peer refresh** — in remote mode, `peer_refresh()` re-globs the
   discovery files and updates the `peer` tool description with the current
   sibling instances + busy states (rebuild only when the set actually
   changed).
4. **`agent.compact()`** – context management (see below).
5. **`agent.stream()`** – streaming request to the inference server; returns
   agent text and optional tool calls. Peer turns pass a dynamic toolset:
   `peer_turn_tools(hops)` (see peer-mechanism.md).
6. **No calls** → the turn ends with the agent reply (a reasoning-only turn
   with no visible output gets an explicit info marker in the TUI).
7. **Calls present** → `agent.run_calls()`: parallel execution, results into
   the context, then the next step.

### Parallelism (`ThreadPoolExecutor`)

Multiple tool calls of one step run in parallel (bash, grep, read_file ...);
workers scale with online swarm agents (see swarm.md). Each call is timed;
wall time vs. sum of per-call times lands in the trace as a parallelism
metric.

### Stagnation Check

If the same call (`name` + args digest + result digest) executes identically
three times in a row, the turn is aborted with "Stagnation" — this prevents
endless loops where a tool keeps returning the same result.

## The Agent Loop (`Agent`, modules/core/agent.py)

### `stream()` – Streaming

SSE parsing (`modules/core/inference.py`), split into three block kinds:
`think` (reasoning / `think` tags), `agent` (main text), `tool`. Tool calls
are assembled incrementally from `delta.tool_calls`. The final chunk's
`usage` updates the real context size (`ctx_used`) and completion tokens for
the trace. **Only the connection phase is retried** (`_retry`, up to
`RETRY_ATTEMPTS = 8` attempts, exponential backoff capped at
`RETRY_MAX_WAIT = 10` s; 5xx retried, 4xx not) — a mid-stream error would
duplicate content that is already displayed.

### `compact()` – Context Compression

Keeps the context under 80 % of the window (real `prompt_tokens`, else
character estimate):

1. **First pass (cheap):** oversized tool results are trimmed in place
   (`trim_tool_results`, no LLM round trip). If that alone frees enough
   context (decided on the REAL usage, scaled by the freed chars), the
   expensive head-summary is skipped entirely — a single huge recent tool
   result would otherwise re-trigger `compact()` every step.
2. **If not enough:** the old head (`m[1:cut]`, never splitting a
   tool-call pair) is replaced by an LLM summary. Trimmed original lines
   stay in the DB (`compacted=1`) so `recall()` can still find them.

### Sub-Agents: `delegate` / `consult`

- **`delegate()`** – its own tool loop (max 10 steps) on a swarm agent with
  its own context but the same (sub-)tools. If the agent fails mid-run due
  to network/server errors, the main agent takes over the task itself
  (`delegate_fallback`, local model, same nesting depth). Progress is shown
  live in the block header (step + elapsed time).
- **`consult()`** – multi-round consultation: the remote model may ask up to
  3 follow-up questions (`QUESTION:`), the local agent answers with its full
  context. Ends when the model is satisfied (`ANSWER:`). Old tool results
  are trimmed to 2000 chars to avoid context bloat.

Both use `SUB_TOOLS` (tool list minus `delegate`/`consult`/`peer`) — this
prevents recursion and 409 loops. Peer turns are different: they use
`peer_turn_tools(hops)` (see peer-mechanism.md, Recursion Guard), which keeps
`delegate`/`consult` when `PEER_TURN_SUBAGENTS` is on and re-adds `peer`
while `hops < PEER_MAX_HOPS`.

### Tool Dispatch

`_TOOL_FNS` is a dict of small single-purpose methods per tool (`bash`,
`grep`, `read_file`, `write_file`, `edit_file`, `recall`, `delegate`,
`consult`, `peer`). `run_tool()` parses the args, looks up the function and
catches errors (`error: ...`). Known tools without a handler → "unknown
tool". Tool implementations live in `modules/io/tools.py` (see tools.md).

## Turn Trace (Metrics)

Every turn writes one row into the `traces` table
(`modules/core/sessiondb.py`) — structured data for A/B comparisons and self
improvement: steps, tools, errors, chars, ms, per-tool breakdown,
delegations, parallelism, `prompt`/`completion` tokens, latency. The trace
is written in the turn's `finally` and must never break the turn itself
(disk full etc.).
