"""Key handling: key dispatch, TAB completion, history navigation,
CSI sequences, raw input feed.

Mixin for the App class (necro.py) - pure logic, no TUI draw calls, so it
stays in the ui layer with no dependencies beyond the stdlib (layer rule:
ui imports only core.palette).
"""
import os, re, shutil, time


class KeyHandler:
    """Key handling for the App (mixin): expects the App state (buf, cur, rx,
    menu, history, ...) and the App methods ins/submit/abort to exist."""
    def key(self, c):
        b, cur = self.buf, self.cur
        if c in "\r\n":
            self.submit()
        elif c in "\x7f\x08":
            if cur:
                self.buf, self.cur = b[:cur - 1] + b[cur:], cur - 1
                self.tab_hint, self.menu = "", None
        elif c == "\x03":
            self.buf, self.cur, self.tab_hint, self.menu = "", 0, "", None  # also clear the input line
            self.abort() if self.busy else self.clear_session()  # idle: clear screen (like /clear)
        elif c == "\x1b":  # bare Escape (no CSI bytes followed) also aborts
            if self.busy:
                self.abort()
            elif self.menu:
                self.menu, self._tab_cycle = None, None  # close popup + reset cycle
        elif c == "\x04":
            self.quit = self.quit or not b
        elif c == "\x14":
            self.think = not self.think
        elif c == "\x0f":
            self.show_tools = not self.show_tools
        elif c == "\x18":
            self.show_edits = not self.show_edits
        elif c == "\x09":
            self.tab_complete()
        elif c >= " ":
            self.ins(c)
    def _tab_apply(self, cands, typed_dir, name, start, cur, b, single_extra=""):
        """Shared TAB cycle logic (commands and files): no-match popup, single-candidate
        insert (+ single_extra), longest common prefix, then cycling."""
        if not cands:  # no match: popup only (no text change)
            self._tab_cands, self._tab_cycle, self.menu, self.menu_sel = None, None, ["(no matches)"], 0
            return
        cyc = self._tab_cycle  # the inserted candidate becomes the only match; keep cycling
        if cyc and cands[0] not in cyc:
            cyc = None
        if self._tab_cands != cands:  # a fresh round resets the counter (-1: first TAB -> cand 0)
            self._tab_i = -1
        self._tab_cands = cands
        if len(cands) == 1 and cyc and len(cyc) > 1:  # mid-cycle: continue through the list
            i = (cyc.index(cands[0]) + 1) % len(cyc)
            full = typed_dir + cyc[i]  # no trailing '/': cycling stays on this directory level
            self.buf, self.cur = b[:start] + full + b[cur:], start + len(full)
            self._tab_cycle = cyc
            self.menu, self.menu_sel = cyc, i
            return
        self._tab_cycle = None
        if len(cands) == 1:  # single candidate: insert (+ single_extra: ' ' for commands, '/' for dirs)
            full = typed_dir + cands[0] + single_extra
            self.buf, self.cur = b[:start] + full + b[cur:], start + len(full)
            self.menu = None
            return
        prefix = os.path.commonprefix(cands)
        if len(prefix) > len(name):  # first TAB: longest common prefix
            full = typed_dir + prefix
            self.buf, self.cur = b[:start] + full + b[cur:], start + len(full)
            self.menu, self.menu_sel = cands, 0
        else:  # subsequent TABs: cycle through the candidates (no trailing '/': stay on this level)
            self._tab_i += 1  # was never incremented (always cand 0)
            full = typed_dir + cands[self._tab_i % len(cands)]
            self.buf, self.cur = b[:start] + full + b[cur:], start + len(full)
            self._tab_cycle = cands  # remember the list for the mid-cycle TABs
            self.menu, self.menu_sel = cands, self._tab_i % len(cands)  # popup follows the cycle
    def tab_complete(self):
        """bash-like TAB completion: '/' commands (single slash) and file names
        (relative to cwd); a unique directory gets a trailing '/'."""
        b, cur = self.buf, self.cur
        start = cur
        while start > 0 and b[start - 1] not in " \t":
            start -= 1
        word = b[start:cur]
        if not word:
            return
        if word.startswith("/") and word.count("/") == 1:  # internal command
            cmds = ("/help", "/quit", "/clear", "/new", "/model", "/cd", "/review", "/test", "/plan")
            self._tab_apply([c for c in cmds if c.startswith(word)], "", word, start, cur, b, single_extra=" ")
            return
        d, name = os.path.split(word)
        d = d or "."
        try:
            # .lock files are 0-byte flock artifacts (file_lock), not real user
            # files - exclude them so TAB completes to the data file, not the lock.
            entries = [e for e in os.listdir(d) if not e.startswith(".") and not e.endswith(".lock")]
        except OSError:
            return  # unreadable dir: leave the word alone
        cands = [e for e in entries if e.startswith(name)]
        # the directory prefix exactly as typed ('./', 'sub/', or '') - os.path.split
        # normalizes './x' to d='.', so the typed prefix must be kept separately or TAB
        # would drop it (the user could only ever pick the first candidate of a browse).
        typed_dir = word[:word.rfind("/") + 1] if "/" in word else ""
        single_extra = "/" if len(cands) == 1 and os.path.isdir(os.path.join(d, cands[0])) else ""
        self._tab_apply(cands, typed_dir, name, start, cur, b, single_extra)
    def hist_nav(self, up):
        """Arrow up/down walks the prompt history (terminal-like). Up starts from
        the newest entry (even over a user-typed line - the user asked for the
        arrows to be history-only, no content scroll); Down walks back to the
        present. Returns True when an entry was loaded, False at a boundary."""
        if not self.history:
            return False
        i = max(0, min(self._hist_i + (-1 if up else 1), len(self.history)))
        if i == self._hist_i:
            return False
        self._hist_i = i
        self.buf = self.history[i] if i < len(self.history) else ""  # i=len = present, i=len-1 = newest
        self.cur = len(self.buf)
        return True
    def csi(self, p, f):
        # one page = the visible content area (H - 9: 2 header + 1 separator +
        # 6 footer lines); H - 5 scrolled 4 lines past the viewport per press
        page = max(shutil.get_terminal_size((80, 24)).lines - 9, 1)
        if f == "A":
            self.hist_nav(True)  # arrows = history only; content scroll is PgUp/PgDn/wheel
        elif f == "B":
            self.hist_nav(False)
        elif f == "C":
            if self.menu and len(self.menu) > 1:  # popup open: arrows scroll the menu
                self.menu_sel = (self.menu_sel + 1) % len(self.menu)
            else:
                self.cur = min(len(self.buf), self.cur + 1)
        elif f == "D":
            if self.menu and len(self.menu) > 1:
                self.menu_sel = (self.menu_sel - 1) % len(self.menu)
            else:
                self.cur = max(0, self.cur - 1)
        elif f == "H" or (f == "~" and p in ("1", "7")):
            self.cur = 0
        elif f == "F" or (f == "~" and p in ("4", "8")):
            self.cur = len(self.buf)
        elif f == "~" and p == "5":
            self.scroll += page
        elif f == "~" and p == "6":
            self.scroll = max(0, self.scroll - page)
        elif f == "~" and p == "3":
            self.buf = self.buf[:self.cur] + self.buf[self.cur + 1:]
    def feed(self, s):
        self.rx += s
        self.rx_time = time.time()
        while self.rx:
            r, c = self.rx, self.rx[0]
            if c != "\x1b":
                self.rx = r[1:]
                self.key(c)
                continue
            if r.startswith("\x1b[200~"):  # bracketed paste
                e = r.find("\x1b[201~")
                if e < 0:
                    return
                self.ins(r[6:e].replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    "))
                self.rx = r[e + 6:]
            elif len(r) == 1:
                return
            elif r[1] in "[O":
                m = re.match(r"\x1b[\[O]([0-9;?]*)([A-Za-z~])", r)
                if not m:
                    if len(r) < 12:
                        return  # incomplete sequence
                    self.rx = r[1:]
                    continue
                self.csi(m[1], m[2])
                self.rx = r[m.end():]
            else:
                if r[1] in "\r\n":  # Alt+Enter = newline
                    self.ins("\n")
