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
- **Empty allow-list = nobody** (deny-all). A bot without a whitelist
  answers no one.
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
| `/quit` | Quit NECRO (aborts a running turn first) |
| `/status` | Token/context usage (real, or a labelled estimate) - answered immediately, also mid-turn |
| `/help` | This list |
| `/tgcommands [clear]` | (Re)push / remove the Telegram client command menu (`setMyCommands`) |
| `<text>` | Normal prompt (starts a turn) |

Anything that is not a command is a prompt.

The command list is a single source (`TG_COMMAND_SPECS` in `necro.py`): the
`/help` text is derived from it, and the same list is pushed to Telegram at
startup via `setMyCommands` (best effort — a failure only logs a warning).
Telegram shows the entries in its own `/` menu. Descriptions are cut to 25
chars (Bot API hard limit).

## Progress feedback

- **Typing indicator:** sent at turn start and refreshed by a ticker thread
  every ~4 s (the indicator expires after ~5 s on the client side).
- **Milestones:** one message every `TG_MILESTONE_EVERY` = 20 steps
  (`⏳ milestone N: step X/25`) — a long turn stays visible without spam.
- The final reply is sent as `✅ done` + the agent text.

## File exchange

- **Out:** `send_file(chat_id, path, caption)` — `sendDocument`, any file type,
  50 MB Bot API limit. Long text (> 4000 chars) is sent as a `.md` document.
- **In:** incoming `document` / `photo` (largest size) messages are downloaded
  to `/tmp/necro-tg/<epoch>_<name>` via `getFile` + fetch, then routed as a
  prompt `[telegram file] <path> - <caption>`. The agent reads the file with
  its normal file tools. Filenames are untrusted: basename only (no path
  traversal). Download failure drops the message silently (no crash).
  Media-only messages (no caption) are routed too.

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
