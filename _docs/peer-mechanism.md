# NECRO – Peer Mechanism

Sibling NECRO instances can delegate full agent turns to each other via a
lightweight local HTTP API (`modules/io/api.py`). This enables parallel
execution across containers or hosts without external orchestration. The
`peer` tool and its description refresh live in `necro.py`
(`_tool_peer` in `modules/core/agent.py`).

## Architecture

Each NECRO instance optionally runs a **local HTTP server**
(`ThreadingHTTPServer`) exposing three endpoints (all token-checked):

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | Liveness check — returns `{id, busy}` |
| `/api/prompt` | POST | Execute a full agent turn on the peer |
| `/api/claim` | POST | Parent-child topology hint (`{child, op: claim\|release}`) |

The server binds to `127.0.0.1` by default (ephemeral port, `port=0`). For
Docker/container setups: `peer_host=0.0.0.0` + a fixed, exposed port. The
**master switch is `remote=true`** (cfg) / `NECRO_REMOTE` (env) — when off,
the server is not started and the `peer` tool is not registered. `peering` /
`NECRO_PEERING` is the narrow switch (Peer-API only, no cfg swarm).

## Discovery

Peers discover each other via **discovery files** in `/tmp/necro/`
(`necro-{id}.json`, configurable via `NECRO_PEER_DIR`):

1. **Startup:** each instance writes its file atomically (tmp +
   `os.replace`, mode 0600 — it carries the token):
   `{id, host, ip, port, token, peer_turn_timeout, pid, cwd, model?, url?}`.
   `model`/`url` are optional (older instances lack them); they are
   refreshed after a `/model` switch so a parent never sees a stale model
   name.
2. **Lookup:** `peer_instances()` globs all `necro-*.json` files and checks
   reachability:
   - **Local peers** (same IP): PID liveness check via `os.kill(pid, 0)`;
     a dead PID → the file is unlinked.
   - **Cross-container peers** (different IP): HTTP `/api/health` (PID
     namespaces differ in Docker). Success is cached 5 s (`ALIVE_TTL`); a
     failed check is NOT cached (the grace-period logic must see consecutive
     failures). A single failure only soft-excludes the peer; the file is
     hard-unlinked only after `PEER_GRACE_PERIOD` = 30 s of consecutive
     failures.
3. **Own identity:** `OWN_ID` (random 8 hex chars) — the PID is NOT unique
   across containers, so self-filtering uses the id, not the PID.

## Authentication

Each instance generates a random **token** (`os.urandom(12).hex()`), stored
in the discovery file. Every request must carry it in the `X-Necro-Token`
header — including `/api/health` (an unauthenticated health endpoint would
leak id + busy state to the LAN when bound to `0.0.0.0`). Missing or wrong
token → `401`.

## Turn Execution (`/api/prompt`)

When the agent invokes the `peer` tool:

1. **Target selection** (`_tool_peer`): candidates = discovered instances
   minus own id, minus the direct caller (no bounce back), minus claimed
   grandchildren (see below). An explicit `instance` that is a claimed
   grandchild gets a clear error. Without an explicit target, **idle peers
   are preferred** (busy status cached 5 s, `PEER_BUSY_TTL`).
2. **Pre-flight** `peer_health()` — reachability check before sending a big
   task. No busy check: the peer queues the turn.
3. **Claim:** if this is itself a peer turn (the sender is my parent), the
   parent is notified `claim` for the target — best-effort, 2 s timeout.
4. **Dedup:** an identical in-flight peer call (same target + task digest)
   is waited on, not re-fired — the model sometimes emits the same call
   twice in one step and `run_calls` fires both in parallel. A generation
   counter closes the stale-result race (a waiter can only wake on the run
   it observed starting).
5. **POST** with `{task, from, hops, budget_left?}`. The task is prefixed
   `You are sibling of [<parent id>] – don't peer back, I'm waiting for
   your final answer.`
6. **Caller timeout:** the PEER decides how long its turn may run — its
   discovery file carries its own `peer_turn_timeout`. The caller timeout is
   `2 * peer_turn_timeout + 30` (queue wait + turn), capped by the remaining
   chain budget.
7. **On the peer:** the task is appended as a `user` message (under the
   lock) and **enqueued** into `_peer_queue`. A worker thread drains it one
   turn at a time, in order, and waits for a running local turn to finish
   first (two turns never race on `app.messages`). A second POST no longer
   gets `409` — it blocks until the turn ran.
8. **Watchdog:** the peer turn is capped at `min(peer_turn_timeout,
   budget_left)` — a hung turn must not hold `busy` forever. The watchdog
   marks itself (`_watchdog_fired`) so the abort reason reads "turn timeout
   (watchdog, N s)" instead of a bare "aborted".
9. **Result:** `{result: text, tool_calls: count}`. New messages of the
   delegated turn are identified by **object identity** (`_last_reply`, set
   in the turn loop) and `tool_call_id` (stable across dict swaps during
   `trim_tool_results()`) — only messages created during THIS turn are
   returned. No reply this turn → `(aborted: <reason>)` when the turn
   recorded an error, else `(no output)`.

## Chain Budget (multi-hop)

A multi-hop chain shares **one wall-clock budget**, not one full
`peer_turn_timeout` per hop:

- The chain start time (`_chain_t0`) and total budget (`_chain_budget`) are
  stored on the app by the peer-turn worker.
- A peer call made DURING a peer turn passes the remaining seconds as
  `budget_left` to the next hop; the receiver's watchdog is capped by it.
- `hops` = hop number of the receiving turn (1 = direct call from a local
  turn). The `peer` tool is only re-added while `hops < PEER_MAX_HOPS`
  (default 1 = no multi-hop; set 2+ for parent-child chains).

## Parent-Child Tree (Claims)

Peering is parent-child, not flat: a source must not address a
**grandchild** directly (route via the child, not around it).

1. When node X (a peer turn, i.e. `_peer_from` is set) calls node Y, X
   notifies its own parent P: `POST /api/claim {child: Y, op: claim}`.
2. P records the claim (`_PEER_CLAIMS`) and **excludes Y from its peer
   list** — both in the tool description (`peer_refresh`) and in
   `_tool_peer` routing. An explicit target on a claimed grandchild gets a
   clear error.
3. When Y's turn finishes (or fails), X sends `op: release` — Y becomes
   directly addressable again. (Only the DEDUP RUNNER releases, never a
   waiter.)

Claims are **best-effort** (2 s timeout, no retry): a lost notification only
weakens the topology hint, it never breaks the chain. Backstops:

- **TTL** (`PEER_CLAIM_TTL`, derived as `max(900, turn_timeout + 600)`): a
  crashed claimer cannot hold a claim forever; stale entries are cleaned in
  `peer_excluded_ids()` (called on every peer call — the natural cleanup
  point).
- **Discovery:** dead peers drop out of the list anyway, so a claim on a
  dead id is inert.

Own children are NOT excluded for the parent (re-calling a child just queues
behind its running turn — still parent-child).

## Recursion Guard

Peer turns use `peer_turn_tools(hops)` — the full tool list **minus**
`delegate`, `consult`, and `peer`, **plus** `peer` again while
`hops < PEER_MAX_HOPS` (multi-hop chains, capped) and `delegate`/`consult`
when `PEER_TURN_SUBAGENTS` is on (default). A peer can therefore delegate to
swarm agents and (up to the hop cap) to further peers; unbounded recursion
is prevented by the hop cap + chain budget, not by removing the tools.

## Tool Description Refresh

`peer_refresh()` runs every agent step in remote mode and rebuilds the
`peer` tool description with the current instances:
`id (pid N, cwd, idle|busy, model @ url)`. A signature guard rebuilds only
when the instance set or a busy state actually changed (rebuilding = glob +
per-peer HTTP health + string rebuild — all wasted work when nothing moved).

## Configuration

| Setting | Default | Description |
|---|---|---|
| `NECRO_PEER_DIR` | `/tmp/necro` | Directory for discovery files |
| `peer_host` (cfg) | `127.0.0.1` | Bind address |
| `peer_port` (cfg) | `0` | Port (`0` = ephemeral) |
| `remote` / `NECRO_REMOTE` | `false` | **Master switch** — Peer-API + cfg swarm |
| `peering` / `NECRO_PEERING` | `false` | Peer-API/peer tool only (no cfg swarm) |
| `peer_turn_timeout` | `600` | Max wall time of a peer turn in s (watchdog + HTTP waits; published in the discovery file, the PEER's value is used by callers) |
| `peer_max_hops` | `1` | Max peer hops in one chain (1 = no multi-hop) |
| `peer_turn_subagents` | `true` | `delegate`/`consult` inside peer turns |

## Error Handling

- **Bind failure** (`EADDRNOTAVAIL`): falls back to `0.0.0.0` + ephemeral
  port. **Port in use** (`EADDRINUSE`): keeps the host, switches to an
  ephemeral port. Either way a bad peer config must not kill startup; a
  warning is logged.
- **Busy peer:** the task is queued and runs after the current turn (no
  `409`).
- **Turn timeout:** the peer returns whatever was produced so far, or
  `(aborted: turn timeout (watchdog, N s))` / `(no output)`.
- **Offline peer:** `peer_instances()` drops it (grace period), the tool
  description shows fewer instances, and a call to a vanished id returns
  "no sibling NECRO instance found".
