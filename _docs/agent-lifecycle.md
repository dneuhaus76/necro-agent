# NECRO – Agent and Turn Lifecycle

The core of NECRO is the agent loop: one LLM turn that alternates between
inference (streaming) and tool execution until the model requests no more
tools or an abort condition triggers. The pure logic lives in
`modules/core/agent.py` (no TUI); the flow controller (`turn()`) with the
safety breaks and the trace lives in `necro.py`.

## Architecture

```
App.turn()  (necro.py)          Agent (modules/core/agent.py)
      │                                │
      │  for step in range(MAX_STEPS)   │
      ├── agent.compact() ─────────────►│   context < 80 % of the window
      ├── agent.stream(tools) ─────────►│   SSE stream -> text + tool calls
      │      │                         │
      │      ▼                         │
      ├── agent.run_calls(calls) ──────►│  parallel tool execution
      │      │                         │
      │      └─ no calls? ─────────────► turn ends (agent reply)
```

## The Turn (`App.turn`, necro.py)

A turn is a single user request. It runs in its own thread (`_start_turn`)
and repeats up to `MAX_STEPS` steps:

1. **Safety breaks** (checked per step):
   - `self.stop` (user abort, e.g. `^c` / `/stop`) -> immediate abort.
   - `TURN_TIMEOUT` (wall clock, default 3600 s) -> `TimeoutError`.
   - `MAX_TURNS` (per session, default 200) -> turn ends, `/new` required.
2. **`agent.compact()`** – context management (see below).
3. **`agent.stream()`** – streaming request to the inference server; returns
   agent text and optional tool calls.
4. **No calls** -> the turn ends with the agent reply.
5. **Calls present** -> `agent.run_calls()`: parallel execution, results into
   the context, then the next step.

### Parallelism (`ThreadPoolExecutor`)

Multiple tool calls of one step run in parallel (bash, grep, read_file ...).
Each call is timed; wall time vs. sum of per-call times lands in the trace as
a parallelism metric.

### Stagnation Check

If the same call (`name` + args digest + result digest) executes identically
three times in a row, the turn is aborted with "Stagnation" – this prevents
endless loops where a tool keeps returning the same result.

## The Agent Loop (`Agent`, modules/core/agent.py)

### `stream()` – Streaming

SSE parsing (`modules/core/inference.py`), split into three block kinds:
`think` (reasoning / `think` tags), `agent` (main text), `tool`. Tool calls
are assembled incrementally from `delta.tool_calls`. **Only the connection
phase is retried** (`_retry`, up to `RETRY_ATTEMPTS = 8` attempts,
exponential backoff capped at `RETRY_MAX_WAIT = 10` s) – a mid-stream error
would duplicate content that is already displayed.

### `compact()` – Context Compression

Keeps the context under 80 % of the window (real `prompt_tokens`, else
character estimate):

1. **First pass (cheap):** huge tool results are trimmed in length – no LLM
   round trip. If that alone is enough, the turn is done and `compact()` is
   not triggered again on the next step.
2. **If not enough:** the old head (`m[1:cut]`, never splitting a
   tool-call pair) is replaced by an LLM summary. Trimmed original lines stay
   in the DB (`compacted=1`) so `recall()` can still find them.

### Sub-Agents: `delegate` / `consult`

- **`delegate()`** – its own tool loop (max 10 steps) on a swarm agent with
  its own context but the same (sub-)tools. If the agent runs through due to
  network/server errors, the main agent takes over the task itself
  (`delegate_fallback`, local model). Progress is shown live in the block
  header (step + elapsed time).
- **`consult()`** – multi-round consultation: the remote model may ask up to
  3 follow-up questions (`QUESTION:`), the local agent answers with its full
  context. Ends when the model is satisfied (`ANSWER:`). Old tool results are
  trimmed to 2000 chars to avoid context bloat.

Both use `SUB_TOOLS` (tool list minus `delegate`/`consult`/`peer`) – this
prevents recursion and 409 loops.

### Tool Dispatch

`_TOOL_FNS` is a dict of small single-purpose methods per tool (`bash`,
`grep`, `read_file`, `write_file`, `edit_file`, `recall`, `delegate`,
`consult`, `peer`). `run_tool()` parses the args, looks up the function and
catches errors (`error: ...`). Known tools without a handler -> "unknown tool".

## Turn Trace (Metrics)

Every turn writes one row into the `traces` table
(`modules/core/sessiondb.py`) – structured data for A/B comparisons and self
improvement: steps, tools, errors, chars, ms, per-tool breakdown,
delegations, parallelism, `prompt`/`completion` tokens, latency.

## History of Key Changes

| Session | Change |
|---|---|
| N167 | Agent loop extracted from `necro.py` into `modules/core/agent.py` (pure logic) |
| N85 | Per-step socket timeout (`step_timeout`) – a hung stream does not block forever |
| F6 | Deterministic `call_{n}` fallback for tool calls (stable across retries) |
| N147 | Telegram progress milestones every 5 steps |
| N151 | Run summary to whitelisted chats on shutdown |
