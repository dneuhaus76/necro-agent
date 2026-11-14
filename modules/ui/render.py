"""NECRO text/Markdown rendering: pure functions, no App state.
Terminal width helpers, SGR styling, inline markdown, word wrap, tables, diffs."""
import difflib, re, unicodedata
# palette (single source of truth in core.palette; necro.py imports these).
# SGR strings are derived from the effective palette; set_palette() rebuilds them
# at startup with cfg 'colors' overrides.
from modules.core import palette as _palette
_PAL = dict(_palette.PALETTE)
def fg(role):
    return "38;5;%d" % _PAL[role]
def bg(role):
    return "48;5;%d" % _PAL[role]
CHOCOLATE, GREY, DARK, RED = fg("user"), fg("grey"), fg("dark"), fg("red")
TELEGRAM = fg("telegram")  # telegram info line
EMBLEM = fg("agent")  # faded dark green (emblem)
_EDIT_OLD = ";" + fg("edit-fg") + ";" + bg("edit-old")  # faded text on edit - lines
_EDIT_NEW = ";" + fg("edit-fg") + ";" + bg("edit-new")  # faded text on edit + lines
_CODE = ";" + fg("code-fg") + ";" + bg("panel-bg")  # code block style
def set_palette(p):
    """rebuild the SGR constants from an effective palette (cfg 'colors')."""
    global CHOCOLATE, GREY, DARK, RED, TELEGRAM, EMBLEM, _EDIT_OLD, _EDIT_NEW, _CODE
    _PAL.update(p)
    CHOCOLATE, GREY, DARK, RED = fg("user"), fg("grey"), fg("dark"), fg("red")
    TELEGRAM = fg("telegram")
    EMBLEM = fg("agent")
    _EDIT_OLD = ";" + fg("edit-fg") + ";" + bg("edit-old")
    _EDIT_NEW = ";" + fg("edit-fg") + ";" + bg("edit-new")
    _CODE = ";" + fg("code-fg") + ";" + bg("panel-bg")
_CW = {}  # per-char width cache - cw() was the hottest function in the profile
         # (2x unicodedata lookups per char); Unicode width semantics are immutable
def cw(ch):
    w = _CW.get(ch)
    if w is None:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            w = 0  # combining / variation selectors (U+FE0F etc.) add no column
        elif unicodedata.east_asian_width(ch) in "WF" or ord(ch) >= 0x1F000 or ch == "\u2712":
            # terminals render SMP emoji (U+1F000+) and the pen ✒ as 2 columns even
            # though Unicode marks them "neutral" (🕷️/✒️ heads were 1 col short)
            w = 2
        else:
            w = 1
        _CW[ch] = w
    return w
def sw(s):
    """Display width of a string (wide/combining chars handled, cached per char)."""
    if s.isascii():  # U+0000-007F has no combining/wide chars -> 1 column/char
        return len(s)
    return sum(cw(ch) for ch in s)
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")  # \x1b]..BEL or \x1b]..ST
def clean(s):
    """Strip control sequences so tool output cannot corrupt the screen. OSC (\x1b]..,
    e.g. a window-title set by a bash command) is removed first - the two-byte ESC
    strip alone would eat only '\x1b]' and leave the payload as visible garbage."""
    s = s.replace("\t", "    ")
    s = OSC.sub("", s)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\x00-\x08\x0b-\x1f\x7f]", "", s)
def chunks(s, w):
    w, res, cur, n = max(w, 1), [], "", 0
    for ch in s:
        c = cw(ch)
        if n + c > w:
            res.append(cur), (cur := ""), (n := 0)
        cur += ch
        n += c
    return res + [cur]
def fit(s, w):
    """Trim s to at most w columns - a line wider than the terminal wraps and
    scrolls the whole screen, desynchronising the incremental draw."""
    if sw(s) <= w:
        return s
    out, n = "", 0
    for ch in s:
        c = cw(ch)
        if n + c > w:
            break
        out += ch
        n += c
    return out
def sgr(base, st):
    """@stable | verified: 2026-11 - frozen: change only on failing test or hard requirement
    Build an SGR escape sequence from a palette role and style flags."""
    s = "\x1b[0;" + base
    s += ";1" * ("b" in st) + ";3" * ("i" in st) + ";4" * ("u" in st)
    s += _CODE * ("c" in st)
    s += _EDIT_OLD * ("o" in st)  # faded dark orange (edit - lines)
    s += _EDIT_NEW * ("g" in st)  # faded dark green (edit + lines)
    s += (";" + fg("red")) * ("e" in st)  # error text overrides the block base color
    return s + "m"
ERR_PREFIX = "error:"
def is_err(text):
    """True when a tool result / system message is an error (convention: it starts
    with 'error:' - see core.agent.run_tool). Used to paint errors in red."""
    return isinstance(text, str) and text.lstrip().lower().startswith(ERR_PREFIX)
def emit(segs, base, pre=""):
    return pre + "".join(sgr(base, st) + t for t, st in segs if t) + "\x1b[0m"
INL = re.compile(r"`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
def inline(t, st=""):
    t, out, p = LINK.sub(r"\1 (\2)", t), [], 0
    for m in INL.finditer(t):
        if m.start() > p:
            out.append((t[p:m.start()], st))
        g = m.group()
        out.append((g[1:-1], st + "c") if g[0] == "`" else
                   (g[2:-2], st + "b") if g.startswith("**") else (g[1:-1], st + "i"))
        p = m.end()
    return out + ([(t[p:], st)] if p < len(t) else [])
def wrap_segs(segs, width):
    """@stable | verified: 2026-11 - frozen: change only on failing test or hard requirement
    Word-wrap styled segments to a fixed width; returns a list of segment lists."""
    width, lines, cur = max(width, 1), [[]], 0
    for text, st in segs:
        for tok in re.findall(r"\s+|\S+", text):
            if tok.isspace():
                if cur and cur < width:
                    lines[-1].append((" ", st))
                    cur += 1
                continue
            w = sw(tok)
            if cur and cur + w > width:
                lines.append([])
                cur = 0
            if w > width:  # hard-split tokens wider than the line
                for part in chunks(tok, width):
                    if cur:
                        lines.append([])
                    lines[-1].append((part, st))
                    cur = sw(part)
            else:
                lines[-1].append((tok, st))
                cur += w
    return lines
def menu_line(items, sel, width):
    """horizontal popup menu - a window of items that fits in `width` cols, the
    selected item is highlighted, '‹'/'›' mark items outside the window."""
    if not items:
        return ""
    budget = max(width, 12) - 2  # reserve one col per side for the arrows
    n, used = 0, 0
    for it in items:
        cost = sw(it) + (2 if n else 0)
        if n and used + cost > budget:
            break
        used += cost
        n += 1
    n = min(n, len(items))
    off = min(max(0, sel - n + 1), max(0, len(items) - n))  # keep the selection visible
    segs = [("‹", "")] if off else []
    for i in range(off, off + n):
        segs.append((items[i], "b" if i == sel else ""))
        if i < off + n - 1:
            segs.append(("  ", ""))
    if off + n < len(items):
        segs.append(("›", ""))
    return emit(segs, DARK)
def diff_view(src, old, new):
    """Numbered diff of one edit (old-file line numbers) for display and model context."""
    start = src[:src.index(old)].count("\n") + 1
    a, b = old.split("\n"), new.split("\n")
    # flush left - marker in col 0, line number in col 2, content in col 8
    out = ["..."] if start > 1 else []
    n = start
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            out += [f"  {n + i:<5} {a[i]}" for i in range(i1, i2)]
            n += i2 - i1
        elif tag == "delete":
            out += [f"- {n + i:<5} {a[i]}" for i in range(i1, i2)]
            n += i2 - i1
        elif tag == "insert":
            # new lines land at the anchor position: number them sequentially
            out += [f"+ {n + (j - j1):<5} {b[j]}" for j in range(j1, j2)]
        else:  # replace: + lines carry the number of the replaced line (sequential)
            out += [f"- {n + i:<5} {a[i]}" for i in range(i1, i2)]
            out += [f"+ {n + (j - j1):<5} {b[j]}" for j in range(j1, j2)]
            n += i2 - i1
    return "\n".join(out)
def table_lines(rows, width, base):
    """Render a Markdown table block: bold header, dash rule, padded data rows.
    The separator row (|---|---|) is consumed, not displayed."""
    def cells(ln):
        s = ln.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]
    parsed = [cells(ln) for ln in rows]
    ncol = max(len(r) for r in parsed)
    parsed = [r + [""] * (ncol - len(r)) for r in parsed]
    is_sep = lambda r: any(c for c in r) and all(re.fullmatch(r":?-+:?", c) for c in r if c)
    header = next((r for r in parsed if not is_sep(r)), [])
    body = [r for i, r in enumerate(parsed) if r is not header and not is_sep(r)]
    widths = [max(sw(r[c]) for r in [header] + body) for c in range(ncol)]
    total = sum(widths) + 2 * (ncol - 1)
    if total > width:  # shrink columns to fit the terminal
        budget = max(width - 2 * (ncol - 1), ncol)
        widths = [max(1, int(w * budget / max(total - 2 * (ncol - 1), 1))) for w in widths]
    def fmt(r, style):
        txt = "  ".join(fit(r[c], widths[c]).ljust(widths[c]) for c in range(ncol)).rstrip()
        return emit([(txt, style)], base)
    out = [fmt(header, "b")]
    out.append(emit([("  ".join("─" * w for w in widths).rstrip(), "")], DARK))
    out += [fmt(r, "") for r in body]
    return out
HEAD_EMOJI = {1: "\U0001F4CC ", 2: "\U0001F538 "}  # only top levels: a marker on every heading would be noise
TASK = {" ": "\u2610", "x": "\u2611"}  # "- [ ]" / "- [x]" checkboxes; a checklist marker is a symbol, not emoji
def md_lines(text, width, base):
    out, code, rows, i = [], False, text.split("\n"), 0
    while i < len(rows):
        ln = rows[i]
        i += 1
        s = ln.strip()
        if s.startswith("```"):
            code = not code
            continue
        if code:
            for ch in chunks(ln, width - 2):
                out.append(emit([("  " + ch + " " * (width - 2 - sw(ch)), "c")], base))
        elif (m := re.match(r"\s{0,3}(#{1,6})\s+(.*)", ln)):
            pre = HEAD_EMOJI.get(len(m[1]), "")  # keeps the level signal on continuation lines
            for k, l in enumerate(wrap_segs(inline(m[2], "bu" if len(m[1]) < 3 else "b"), width - sw(pre))):
                out.append(emit(l, base, pre if k == 0 else " " * sw(pre)))
        elif re.fullmatch(r"\s*([-*_])\1{2,}\s*", ln):
            out.append(emit([("─" * width, "")], DARK))
        elif (m := re.match(r"(\s*)([-*+]|\d+[.)])\s+(.*)", ln)):
            mark, body = None, m[3]
            if (t := re.match(r"\[([ xX])\]\s+(.*)", body) if m[2] in "-*+" else None):
                mark, body = TASK[t[1].lower()], t[2]  # task list: checkbox replaces the bullet
            pre = " " * len(m[1]) + (mark or ("•" if m[2] in "-*+" else m[2])) + " "
            for k, l in enumerate(wrap_segs(inline(body), width - sw(pre))):
                out.append(emit(l, base, pre if k == 0 else " " * sw(pre)))
        elif (m := re.match(r">\s?(.*)", ln)):
            out += [emit(l, base, "│ ") for l in wrap_segs(inline(m[1], "i"), width - 2)]
        elif s.startswith("|") and "|" in s[1:]:
            j = i  # collect the consecutive table block (current line is rows[i-1])
            while j < len(rows) and rows[j].strip().startswith("|") and "|" in rows[j].strip()[1:]:
                j += 1
            if j - i + 1 >= 2:
                out += table_lines(rows[i - 1:j], width, base)
                i = j  # next loop top: ln = rows[j], i becomes j+1
                continue
            out += [emit(l, base) for l in wrap_segs(inline(s), width)]
        elif not s:
            if out and out[-1] != "":
                out.append("")
        else:
            out += [emit(l, base) for l in wrap_segs(inline(s), width)]
    return out
