# NECRO – TUI and Rendering

The terminal UI lives in `necro.py` (App class, `draw()`/`build()`/`render()`)
plus two modules: `modules/ui/keys.py` (input handling, mixin) and
`modules/ui/render.py` (pure text/Markdown rendering functions).

## Terminal Setup

- **Alternate screen** (`?1049h`), **raw mode**, **bracketed paste** (`?2004h`).
- **Cursor shape**: steady underline (`DECSCUSR`, `\x1b[4 q`) instead of the
  default block; restored on exit.
- Input is decoded incrementally (UTF-8, `replace`); a pending lone ESC is
  resolved after a short timeout (20 ms busy / 60 ms idle) as "abort".

## Screen Layout

```
row 1:  header 1  (skull + version | model · think:… · baseurl | step counter)
row 2:  header 2  (/help hint)
row 3:  ─ separator ─
rows 4..ch:  content (blocks, scrollable viewport)
y:      hint line (TAB popup menu, or transient tab_hint)
y+1:    ─ separator ─
y+2..:  input line(s) – wrapped at the right edge (pi-style), '›' or spinner
y+2+n:  ─ separator ─
y+3+n:  cwd line (git branch; flashes 3 s after a cwd change)
y+4+n:  status line (left: ctx % / window; right: scroll + swarm on/off)
```

`ch` (content height) = `H - 9 - (n_in - 1)` – wrapped input lines take room
from the content area.

## Blocks

`App.blocks` is an ordered list of `Block(kind, text, color)`. Kinds:

| Kind | Rendering |
|---|---|
| `user` | `›` prefix, CHOCOLATE; `[telegram:…]` blocks in TELEGRAM color; peer lines (`💀 peer`) in EMBLEM without the `›` |
| `agent` | Markdown (`md_lines`), GREY |
| `think` | dim, `┊ ` gutter, italic; hidden behind the ^T toggle (collapsed to one `🧠 thinking…` line) |
| `tool` | emoji head (cfg `tool_emoji`, default `›`); body behind the ^O toggle; edit diffs flush left with `-`/`+` styling; `recall` gets a compact italic `recall…` head |
| `pre` | preformatted (emblem), no wrap, EMBLEM |
| `info` / `error` | DARK / RED (or a per-block color override) |

Blocks are rendered with a **cache key** `(width, len(text), toggles, color)`
– unchanged blocks skip re-rendering.

## Incremental Draw

A token tick re-rendering the whole screen would flicker on a remote client.
`draw()` therefore:

1. Builds all lines (`build()`) and the footer.
2. Compares every row against `_last_view` (row -> last written line) and
   writes **only changed rows** (`\x1b[r;1H\x1b[2K<line>`).
3. Writes the **cursor row last**, so the physical cursor ends at the input
   text (a write leaves the cursor at the line end).
4. Moves the cursor to the wanted position with a single hide-move-show burst
   (`?25l … ?25h`) – no visible jump, no blink-phase reset. Skipped when the
   cursor line was the last write and the wanted column is its end, or when
   nothing was written and the position is unchanged.
5. **Resize** (SIGWINCH): the `_last_view` cache no longer matches the screen
   – every row is rewritten in the new layout and rows that fall outside it
   are cleared.

The cache is refreshed on **every** draw (not only changed rows): a stale
cache makes the next draw skip a row whose new content equals the old cached
value (e.g. a blank line after scrolling back).

## Spinner (Busy Animation)

While busy and idle (no input, no content change), the main loop calls
`draw(spin_only=True)`: if only the spinner frame/step counter changed, just
the two header lines and the first input line are rewritten – terminal
selection and scrollback survive.

## Scrolling

- `self.scroll` = lines anchored above the viewport; new lines keep the view
  anchored (`scroll += tot - last_tot`), clamped to `[0, tot - ch]`.
- PgUp / PgDn adjust it. One page = the visible content area.

## Input Handling (modules/ui/keys.py)

- **Key dispatch** (`key()`): control keys (`^c` clear/abort, `^d` quit),
  toggles (`^t` thinking, `^o` tools, `^x` edits), ESC abort/close menu.
- **TAB completion** (`tab_complete`): `/commands` (single slash) and file
  names; unique directory gets a trailing `/`; multiple candidates -> popup
  menu + cycling (longest common prefix first).
- **History navigation** (Up/Down): terminal-like; a user-typed line blocks
  the walk, a loaded entry can be walked further.
- **CSI parsing** in `feed()`: bracketed paste, arrows, Home/End, PgUp/PgDn,
  F-keys, Alt+Enter (newline).

## Palette (modules/core/palette.py)

Single Source of Truth for all colors (256-color SGR) and tool emojis.
`resolve(overrides)` applies cfg `colors` (role -> 0-255) at startup and
rebuilds the SGR constants; the named colors (`CHOCOLATE`, `GREY`, `DARK`,
`RED`, `TELEGRAM`, `EMBLEM`, …) are imported **after** `set_palette`, so they
reflect the overrides. Roles are validated against `ROLES` in
`modules/core/config.py`.

## Key Reference (shown by `/help`)

```
^t thinking · ^o tools · ^x edits
↑/↓ history · PgUp/PgDn scroll · Alt+Enter newline
TAB complete (/commands) · Esc abort / close menu · ^c clear (idle) / abort (busy)
/help · /model [name] · /cd <dir> · /clear|/new · /review /test /plan · /quit
```
