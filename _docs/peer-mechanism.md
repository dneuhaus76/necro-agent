# NECRO Peer Mechanism

Sibling NECRO instances can delegate full agent turns to each other via a lightweight local HTTP API. This enables parallel execution across containers or hosts without external orchestration.

## Architecture

Each NECRO instance optionally runs a **local HTTP server** (`ThreadingHTTPServer`) exposing two endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | Liveness check — returns `{id, busy}` |
| `/api/prompt` | POST | Execute a full agent turn on the peer |

The server binds to `127.0.0.1` by default (ephemeral port, `port=0`). For Docker/container setups, `PEER_HOST=0.0.0.0` and a fixed port can be configured.

## Discovery

Peers discover each other via **discovery files** in `/tmp/necro/` (configurable via `NECRO_PEER_DIR`):

1. **Startup**: Each instance writes `necro-{id}.json` containing `{id, host, ip, port, token, pid, cwd}`.
2. **Discovery**: `peer_instances()` globs all `necro-*.json` files and checks reachability:
   - **Local peers** (same IP): PID liveness check via `os.kill(pid, 0)`.
   - **Cross-container peers** (different IP): HTTP `/api/health` request (PID namespaces differ in Docker).
3. **Stale cleanup**: Unreachable peers are removed from `/tmp/necro/`.

## Authentication

Each instance generates a random **token** (`os.urandom(12).hex()`). The calling peer sends it in the `X-Necro-Token` request header. Missing or wrong token → `401`.

## Turn Execution (`/api/prompt`)

When the agent invokes the `peer` tool (e.g. `peer:fd17831a` with a task):

1. `do_POST("/api/prompt")` is called on the target peer.
2. Token verification.
3. Appends the task as a `user` message into the peer's chat context (under the lock).
4. **Enqueues** the turn into the peer's `_peer_queue` – a worker thread drains it one turn at a time, in order, and waits for a running local turn to finish first (so two turns never race on `app.messages`). A second POST no longer gets `409`.
5. **Waits up to `peer_turn_timeout` seconds** (default 600, cfg/env configurable; watchdog) for the result.
6. Returns `{result: text, tool_calls: count}`.

### Parent-Child Tree (N226 claims)

Peering is parent-child, not flat: a source must not address a **grandchild**
directly (route via the child, not around it).

1. When node X calls node Y, X notifies its own parent P (if X is a peer turn,
   i.e. `_peer_from` is set): `POST /api/claim {child: Y, op: claim}`.
2. P records the claim (`_PEER_CLAIMS`) and **excludes Y from its peer list** —
   both in the tool description (`peer_refresh`) and in `_tool_peer` routing.
   An explicit target on a claimed grandchild gets a clear error.
3. When Y's turn finishes (or fails), X sends `op: release` — Y becomes directly
   addressable again.

Claims are **best-effort** (2 s timeout, no retry): a lost notification only
weakens the topology hint, it never breaks the chain. Backstops:

- **TTL** (`PEER_CLAIM_TTL`, 900 s): a crashed claimer cannot hold a claim
  forever (the chain budget is shorter).
- **Discovery**: dead peers drop out of the list anyway, so a claim on a dead
  id is inert.

Own children are NOT excluded for the parent (re-calling a child just queues
behind its running turn — still parent-child).

### Recursion Guard

Peer turns use `SUB_TOOLS` — the full tool list **minus** `delegate`, `consult`, and `peer`. This prevents a delegated peer from recursively calling another peer, which would cause a `409` loop.

### Result Extraction

New messages from the delegated turn are identified by **object identity** (`id(m)`) and **tool_call_id** (stable across dict swaps during `trim_tool_results()`). Only messages created during this turn are returned — stale replies from earlier turns are excluded.

## Configuration

| Setting | Default | Description |
|---|---|---|
| `NECRO_PEER_DIR` | `/tmp/necro` | Directory for discovery files |
| `peer_host` (cfg) | `127.0.0.1` | Bind address |
| `peer_port` (cfg) | `0` | Port (`0` = ephemeral) |
| `REMOTE` (cfg) | `false` | **Master switch** — peer API is disabled by default |

The peer API is only activated when `remote=true` (cfg) or `NECRO_REMOTE` (env). When disabled, the server is not started and the `peer` tool is not registered.

## Error Handling

- **Bind failure** (e.g. `EADDRNOTAVAIL`): Falls back to `0.0.0.0` + ephemeral port. The instance continues running; a warning is logged.
- **Port in use**: Keeps the configured host, switches to an ephemeral port.
- **Busy peer**: The task is queued and runs after the current turn (no `409`).
- **Turn timeout**: The waiting thread is capped at `peer_turn_timeout` (default 600 s, configurable). If the turn does not complete, the result is whatever was produced so far (or `(no output)` if no assistant reply was generated).
