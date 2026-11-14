"""TUI view layer: block rendering + screen drawing (ViewMixin for necro.App).
Pure text helpers live in modules/ui/render.py - this module adds the App state
(blocks, input buffer, scroll position, incremental-draw caches). No config reading:
the values necro.py owns are injected once via configure() (SSOT stays there)."""
import bisect, os, shutil, subprocess, time
from modules.ui import render as _render
from modules.ui.render import (CHOCOLATE, GREY, DARK, RED, TELEGRAM, EMBLEM,
                               cw, sw, clean, chunks, fit, emit, wrap_segs, md_lines, menu_line,
                               is_err)

def configure(**kw):
    """Bind the config values necro.py owns (version, base url, swarm lists, symbols, step cap)."""
    globals().update(kw)

_GIT_BRANCH = None
def git_branch():
    """Current git branch (cached once); '' if not a repo or on error."""
    global _GIT_BRANCH
    if _GIT_BRANCH is None:
        try:
            _GIT_BRANCH = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                         capture_output=True, text=True, timeout=3).stdout.strip()
        except Exception:
            _GIT_BRANCH = ""
    return _GIT_BRANCH

class ViewMixin:
    # ---- rendering
    def render(self, b, w):
        key = (w, len(b.text), self.think, self.show_tools, self.show_edits, b.color)
        if b.key == key:
            return b.lines
        t, k, L = clean(b.text), b.kind, []
        if k == "user":
            lines_t = t.split("\n")
            is_peer = lines_t[0].startswith(PEER_TAG)
            is_tg = lines_t[0].startswith("[telegram")
            for n, ln in enumerate(lines_t):
                for j, l in enumerate(wrap_segs([(ln, "")], w - 2)):
                    sym = "" if (n == j == 0 and is_peer) else (f"\x1b[0;1;m{SYM} " if n == j == 0 else "  ")
                    base = EMBLEM if is_peer else (TELEGRAM if is_tg else CHOCOLATE)  # whole TG block in its color
                    sym_s = f"\x1b[0;{base}m{sym}\x1b[0m" if sym.strip() else sym
                    L.append(sym_s + emit(l, base))
        elif k == "agent":
            L = md_lines(t.strip("\n"), w, GREY)
        elif k == "pre":  # preformatted (emblem): no word wrap, spaces preserved; faded dark green
            for ln in t.split("\n"):
                L += [emit([(c, "")], EMBLEM) for c in chunks(ln, w)]
        elif k == "think":
            # the head line stays visible in both states (expanded = body below it)
            L = [emit([(TOOL_EMOJI["thinking"] + " thinking..." + ("" if self.think else " (Ctrl-t to show)"), "i")], DARK)]  # default via SSOT (palette.EMOJI); cfg 'tool_emoji' overrides
            if self.think:
                for ln in t.strip("\n").split("\n"):
                    L += [emit(l, DARK, "┊ ") for l in wrap_segs([(ln, "i")], w - 2)]
        elif k == "recall":
            # recall renders like the thinking line (gutter + italic) and follows the
            # ^t toggle: it is context lookup, not a tool action the user must audit.
            head, *body = t.rstrip().split("\n")
            q = (head.split(None, 1) + [""])[1].strip()  # "recall  <query>" -> query
            hits = [ln for ln in body if ln.strip()]
            n_hits = 0 if hits and hits[0].startswith("no matches") else len(hits)
            label = f"{TOOL_EMOJI['thinking']} recall {q} · {n_hits} hit{'s' if n_hits != 1 else ''}"
            L = [emit([(label + ("" if self.think else " (Ctrl-t to show)"), "i")], DARK)]
            if self.think:
                for ln in body:
                    L += [emit(l, DARK, "┊ ") for l in wrap_segs([(ln, "i")], w - 2)]
        elif k == "tool":
            head, *body = t.rstrip().split("\n")
            sym = TOOL_EMOJI.get((head.split() or ["?"])[0], SYM_TOOL)  # cfg 'tool_emoji' overrides
            is_edit = head.startswith("edit_file")
            head_segs = [(head, "b")]
            # swarm/peer tools are highlighted in necro green (EMBLEM) instead of dark
            head_color = EMBLEM if (head.split() or ["?"])[0] in ("consult", "peer", "delegate") else DARK
            # an error result (convention: it starts with "error:") is painted red
            err = bool(body) and is_err(body[0])
            shown = self.show_edits if is_edit else self.show_tools
            # a hidden body keeps the head visible with a toggle hint (like the think line)
            if body and not shown:
                head_segs.append((" (Ctrl-x to show)" if is_edit else " (Ctrl-o to show)", "i"))
                if err:  # signal the failure even while the body is collapsed
                    head_segs.append((" · error", "ie"))
            for j, l in enumerate(wrap_segs(head_segs, w - 2)):
                L.append(emit(l, head_color, (sym + " ") if j == 0 else " " * sw(sym)))
            if shown and body:
                for ln in body:
                    st = "e" if err else (("o" if ln.startswith("-") else "g" if ln.startswith("+") else "")
                                          if is_edit else "")
                    # the edit diff is flush left (aligned with the head line);
                    # other tool bodies keep the 2-space indent
                    pre, bw = ("", w) if is_edit else ("  ", w - 2)
                    # edit lines are coloured over the full line width (padded with styled spaces)
                    L += [emit([(c + " " * max(0, bw - sw(c)), st)], DARK, pre) for c in chunks(ln, bw)]
        else:  # info / error
            # per-block color override; errors ("error: ..." convention) are always red
            base = b.color or (RED if k == "error" or is_err(t) else DARK)
            for ln in t.split("\n"):
                L += [emit(l, base) for l in wrap_segs([(ln, "")], w)]
        b.key, b.lines = key, L
        return L
    def build(self, w):
        L = []
        with self.lock:
            for b in self.blocks:
                bl = self.render(b, w)
                if b.kind in ("user", "agent", "think", "tool", "recall") and L and L[-1] != "":
                    L.append("")
                L += bl
        return L
    def input_lines(self, spin, W):
        """The input as wrapped plain lines (pi-style: wrap at the right edge instead
        of a one-line scrolling window). Spinner frame while busy, '›' otherwise
        (same width, no cursor jump); marker and text share CHOCOLATE so the prompt
        does not change color once the user starts typing.
        Returns (lines, cur_row, cur_col): wrapped lines, and the 0-based row /
        1-based col of the cursor within them."""
        text = (spin if self.busy else SYM) + " " + self.buf.replace("\n", "↵")
        width, lines, starts, s0, n = W - 1, [], [0], 0, 0
        for i, ch in enumerate(text):
            c = cw(ch)
            if n + c > width:  # a char that would overflow moves to the next line
                lines.append(text[s0:i]); starts.append(i); s0, n = i, 0
            n += c
        lines.append(text[s0:])
        ci = self.cur + 2  # cursor char index in text (prompt + space prefix)
        row = max(bisect.bisect_right(starts, ci) - 1, 0)
        return lines, row, 1 + sw(text[starts[row]:ci])
    def draw(self, spin_only=False):
        W, H = shutil.get_terminal_size((80, 24))
        spin = SPIN[int(time.time() * 10) % 10] if self.busy else " "
        inp_plain, cur_row, cur_col = self.input_lines(spin, W)
        n_in = len(inp_plain)
        # 2 header lines + 1 separator + footer (hint/sep/input x n_in/sep/cwd/status)
        ch = max(H - 9 - (n_in - 1), 1)  # wrapped input lines take room from the content
        step = f" {self.step}/{MAX_STEPS} " if self.busy else " "
        think_lbl = self.reasoning_effort if self.reasoning_effort else ("on" if self.reasoning else "off")
        h1l = f" 💀 NECRO v{VERSION}"
        h1m = fit(f" · {self.model} · think:{think_lbl} · {BASE}", W - sw(h1l) - sw(step) - 4)
        h1r = f"{step} 💀 "
        panel = _render.bg("panel-bg")  # header / status background (palette role)
        h1 = (f"\x1b[0;1;{EMBLEM};{panel}m{h1l}\x1b[0;1;{_render.fg('header-fg')};{panel}m"
              + f"{h1m}{' ' * max(0, W - sw(h1l) - sw(h1m) - sw(h1r))}{h1r}\x1b[0m")
        h2 = fit(" /help = keys & commands", W)
        h2 = f"\x1b[0;{DARK};{panel}m{h2}{' ' * max(0, W - sw(h2))}\x1b[0m"
        if spin_only and (spin, step, W, self.busy) == self._spin:  # no content change since last full draw
            return
        self._spin = (spin, step, W, self.busy)
        if spin_only:  # spinner frame changed but content did not: refresh only the header
            # lines + first input line so the terminal selection / scrollback survives
            y = ch + 4  # first footer line
            first = emit([(inp_plain[0], "")], CHOCOLATE)
            f = [f"\x1b[1;1H\x1b[2K{h1}",
                 f"\x1b[2;1H\x1b[2K{h2}",
                 f"\x1b[{y + 2};1H\x1b[2K{first}"]
            # keep the incremental-draw cache in sync so the next full draw does not
            # rewrite these rows; the line write leaves the cursor at the line end -
            # restore the wanted position (hide-move-show, only when it differs)
            self._last_view = {**self._last_view, 1: h1, 2: h2, y + 2: first}
            cr, cc = y + 2 + cur_row, cur_col
            if (cr, cc) != (y + 2, 1 + sw(inp_plain[0])):
                f.append(f"\x1b[?25l\x1b[{cr};{cc}H\x1b[?25h")
            self._last_cursor = (cr, cc)  # keep the no-write-move check in sync
            try:
                os.write(1, "".join(f).encode())
            except OSError:
                pass  # dead terminal (EPIPE): the main loop notices on the next read
            return
        L = self.build(W)
        tot = len(L)
        if self.scroll > 0:  # keep the view anchored while new lines arrive
            self.scroll += tot - self.last_tot
        self.last_tot = tot
        self.scroll = max(0, min(self.scroll, max(0, tot - ch)))
        end = tot - self.scroll
        view = L[max(0, end - ch):end]
        est = self.ctx_used or self.ctx_est  # real usage once known, cached estimate before
        # ---- footer: blank / --- / > input (wrapped, n_in lines) / --- / * cwd / * stats
        sep = emit([("─" * W, "")], DARK)
        # ---- status line (pi-style): left ctx usage, right scroll + swarm availability
        pct = min(100.0, est * 100.0 / self.ctx) if self.ctx else 0.0
        left = f"{pct:.1f}%/{self.ctx // 1000}k (auto)"
        # status keyed by (url, model) - duplicate names on different hosts stay separate;
        # online models in necro green (EMBLEM), offline in the standard dark color
        right_segs = [("↑%d " % self.scroll, DARK)] if self.scroll else []
        for a in SWARM:
            up = SWARM_UP.get((a["url"], a["model"]))
            right_segs.append((" %s:%s" % (a["model"], "on" if up else "off"), EMBLEM if up else DARK))
        right = fit(" ".join(t for t, _ in right_segs).strip(), max(W - sw(left) - 1, 1))
        # rebuild the colored segments to match the trimmed plain text (fit may cut mid-segment)
        segs, n, rw = [], 0, sw(right)
        for t, c in right_segs:
            if not t or n >= rw:
                break
            if n + sw(t) > rw:
                segs.append((fit(t, rw - n), c))
                break
            segs.append((t, c))
            n += sw(t)
        right_col = "".join("\x1b[0;%s;%sm%s\x1b[0m" % (c, _render.bg("panel-bg"), t) for t, c in segs)
        right_w = sum(sw(t) for t, _ in segs)  # real width of the colored segments (join spaces not included)
        status = f"\x1b[0;{DARK};{_render.bg('panel-bg')}m{left}{' ' * max(1, W - sw(left) - right_w)}{right_col}\x1b[0m"
        y = ch + 4  # first footer line
        # the horizontal popup menu (TAB candidates) takes the hint line; the
        # transient tab_hint (e.g. "queued - …") shows when no menu is open
        hint = menu_line(self.menu, self.menu_sel, W) if self.menu else \
            (f"\x1b[0;{DARK}m{fit(self.tab_hint, W)}\x1b[0m" if self.tab_hint else "")
        cwd = os.getcwd()
        branch = git_branch()
        cwd_disp = fit(f"{cwd} ({branch})" if branch else cwd, W)
        cwd_line = (f"\x1b[0;{_render.fg('header-fg')};{_render.bg('dark')}m{cwd_disp}{' ' * max(0, W - sw(cwd_disp))}\x1b[0m"
                    if time.time() - self._cwd_flash < 3.0  # cwd changed: flash the line for 3 s
                    else f"\x1b[0;{DARK};{_render.bg('panel-bg')}m{cwd_disp}{' ' * max(0, W - sw(cwd_disp))}\x1b[0m")
        inp_rows = {y + 2 + i: emit([(l, "")], CHOCOLATE) for i, l in enumerate(inp_plain)}
        # incremental draw - a token tick re-renders the WHOLE screen (header + all
        # content + footer); on a remote client that is heavy and flickers. Only rows
        # that actually changed are written; the input rows are written last with the
        # cursor row LAST, so the physical cursor ends at the input text instead of at
        # the end of the status line (which wraps off-screen -> invisible).
        rows = {1: h1, 2: h2, 3: sep}
        for r in range(ch):
            rows[r + 4] = view[r] if r < len(view) else ""
        rows.update({y: hint, y + 1: sep, y + 2 + n_in: sep, y + 3 + n_in: cwd_line, y + 4 + n_in: status})
        f = []
        # a resize (SIGWINCH) reflows the alternate screen, so the _last_view cache
        # no longer matches the screen: force a full rewrite of every row in the new
        # layout and clear rows written at the old layout that fall OUTSIDE it.
        if self._last_wh is not None and (W, H) != self._last_wh:
            old = set(self._last_view) | self._last_input_rows
            for r in sorted(old - set(rows) - set(inp_rows)):
                f.append(f"\x1b[{r};1H\x1b[2K")
            self._last_view = {}
        self._last_wh = (W, H)
        self._last_input_rows = set(inp_rows)
        cur_line = y + 2 + cur_row  # the row holding the cursor - written last
        order = sorted(rows) + [r for r in sorted(inp_rows) if r != cur_line] + [cur_line]
        last_written = None
        for r in order:
            line = rows[r] if r in rows else inp_rows[r]
            if self._last_view.get(r) != line:
                f.append(f"\x1b[{r};1H\x1b[2K{line}")
                last_written = r
        # the cache must be refreshed on EVERY draw, not only when the input lines
        # changed: a stale cache makes the next draw skip a row whose NEW content
        # equals the OLD cached value (e.g. a blank line after scrolling back) -
        # stale text then stays on screen ("compressed" content, missing blank rows)
        self._last_view = {**rows, **inp_rows}
        # cursor: after ANY write the physical cursor sits at the END of the LAST
        # written line - with a busy agent that is a content row, not the input row
        # (the cursor visibly jumped into the content area). Move to the wanted
        # position unless the cursor line was the last write AND the wanted column is
        # its end (then the cursor is already correct). No write at all (e.g. Left/Right
        # only moves cur, the line content is unchanged): the cursor stays where the
        # previous draw left it - move it to the wanted position when that differs
        # (arrow keys in the input line did nothing after a history load).
        # hide-move-show in one burst: no visible jump, no per-tick ?25h blink-phase reset.
        cr, cc = cur_line, cur_col
        end_col = 1 + sw(inp_plain[cur_row])
        if (f and not (last_written == cur_line and cc == end_col)) or \
                (not f and self._last_cursor != (cr, cc)):
            f.append(f"\x1b[?25l\x1b[{cr};{cc}H\x1b[?25h")
        self._last_cursor = (cr, cc)
        if f:
            try:
                os.write(1, "".join(f).encode())
            except OSError:
                pass  # dead terminal (EPIPE): the main loop notices on the next read
