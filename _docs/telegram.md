# NECRO – Telegram Bridge

The Telegram client lives in `modules/io/tg.py` (long polling, stdlib only);
the wiring (whitelist, commands, milestones) lives in `necro.py`. Incoming
text messages become agent turns; the final agent reply is sent back to the
chat.

## Long polling

- `getUpdates` with a 30 s server-side timeout, in a daemon thread.
- **409** (another poller has the updates) → restart from offset 0.
- Network hiccup / flood control → 3 s backoff, then retry.
- Duplicate updates are dropped via a `seen` set of update_ids, bounded at
  1000 entries (oldest evicted) so a long-running bot does not grow memory.

## Chat-ID whitelist

- Source (first match wins): env `NECRO_TELEGRAM_CHATS`, then cfg
  `telegram_chats` — comma-separated chat ids.
- **Empty allow-list = everyone** (open mode, e.g. local testing).
- A message from an unknown chat is answered with
  `(unknown chat <id> - not in the whitelist)` and **no turn is started** —
  no tokens spent. The stranger is parked in a separate queue
  (`stranger_q`), never the prompt queue.
- The allow-list doubles as the recipient list for the start-up greeting.

## Commands

| Command | Effect |
|---|---|
| `/stop` | Interrupt the current turn |
| `/new` | New session (reset context) |
| `/quit` | Quit NECRO |
| `/status` | Context/token usage |
| `/help` | This list |
| `<text>` | Normal prompt (starts a turn) |

Anything that is not a command is a prompt.

## Progress feedback

- **Typing indicator:** sent at turn start and refreshed by a ticker thread
  every ~4 s (the indicator expires after ~5 s on the client side).
- **Milestones:** one message every `TG_MILESTONE_EVERY` = 20 steps
  (`⏳ milestone N: step X/25`) — a long turn stays visible without spam.
- The final reply is sent as `✅ done` + the agent text.

## Busy handling

A prompt arriving while a turn is running is **queued** (`_tg_queued`) and
submitted right after the current turn's `finally` — the user does not lose
the message, and two turns never race on the context.

## Formatting

`telegram_format` (cfg): `markdown` (default) | `html` | `text`.

- **markdown** = MarkdownV2: every reserved char (`\ _ * [ ] ( ) ~ ` > # + -
  = | { } . !`) outside a code span is escaped, or Telegram rejects the
  message. A small tokenizer keeps code spans verbatim and preserves
  bold/italic markers around escaped payloads; unbalanced markers stay
  escaped (render as literals).
- **html:** `& < >` escaped, then `code` → `<code>`, bold/italic → `<b>/<i>`.
- **Fallback:** if the parser rejects a chunk, it is sent as plain text.
- **Long text** (> 4000 chars): sent as a `.md` document (1:1 formatting,
  Telegram hard limit is 4096 chars).
- **Rich tables** (`telegram_rich`, default `auto`): markdown tables are
  detected and routed through `sendRichMessage` (Bot API 10.1); `off` for
  old clients.
