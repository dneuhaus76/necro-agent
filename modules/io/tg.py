"""Telegram client for NECRO (long polling, stdlib only)."""
import http.client, json, mimetypes, os, re, tempfile, threading, time
class TelegramBot:
    """Long-polling Telegram client (pi-telegram-plus-style): incoming text messages
    become agent turns, the final agent reply is sent back. Stdlib only."""
    def __init__(self, token, fmt="markdown", whitelist=(), rich="auto"):
        # fmt = 'markdown' (default, MarkdownV2) | 'html' | 'text'
        # whitelist = chat ids that may spend tokens; messages from other chats
        # go to stranger_q instead of the prompt queue. Empty = everyone.
        # rich = 'auto' (default) | 'off': auto-detect markdown tables and route
        # them through sendRichMessage (Bot API 10.1); off for old clients.
        self.token, self.seen, self.seen_order, self.q, self.fmt = token, set(), [], [], fmt
        self.rich = rich
        self.whitelist, self.stranger_q = list(whitelist), []
        threading.Thread(target=self._loop, daemon=True).start()
    def _api(self, method, **params):
        """One Bot API call; returns the full response dict (result / error_code)."""
        body = json.dumps(params).encode()
        c = http.client.HTTPSConnection("api.telegram.org", timeout=35)
        try:
            c.request("POST", f"/bot{self.token}/{method}", body, {"Content-Type": "application/json"})
            return json.loads(c.getresponse().read())
        finally:
            c.close()
    def _loop(self):
        offset = 0
        while True:
            try:
                r = self._api("getUpdates", offset=offset, timeout=30)
                if r.get("error_code") == 409:  # another poller has the updates: restart from 0
                    offset = 0
                    continue
                for u in r.get("result") or []:
                    offset = u["update_id"] + 1
                    self._route(u)
            except Exception:
                time.sleep(3)  # network hiccup / flood control: retry with backoff
    def _route(self, u):
        """One getUpdates entry -> prompt queue (whitelisted) or stranger queue."""
        msg = u.get("message") or {}
        m = msg.get("text")
        if not m or u["update_id"] in self.seen:
            return
        self.seen.add(u["update_id"])
        self.seen_order.append(u["update_id"])
        if len(self.seen_order) > 1000:  # bound memory on long-running bots
            self.seen.discard(self.seen_order.pop(0))
        chat_id = msg.get("chat", {}).get("id")
        frm = msg.get("from") or {}  # Bot API field is 'from' (a Python keyword, hence frm)
        sender = frm.get("username") or frm.get("first_name") or ""
        if self.whitelist and str(chat_id) not in self.whitelist:
            self.stranger_q.append((chat_id, sender))
        else:
            self.q.append((m.strip(), chat_id, sender))
    def stranger(self):
        """Pop one (chat_id, sender) from the stranger queue, or None."""
        return self.stranger_q.pop(0) if self.stranger_q else None
    @staticmethod
    def _mdv2(text):
        # MarkdownV2 reserves \ _ * [ ] ( ) ~ ` > # + - = | { } . !: every reserved
        # char OUTSIDE a code span must be escaped or Telegram rejects the message.
        # Tokenize the agent's markdown into code spans (`...`), bold (**..** / *..*),
        # italic (_.._) and plain text. Code spans are kept verbatim (literal content);
        # for bold/italic the payload is escaped but the markers survive; plain text is
        # fully escaped. Unbalanced markers simply stay escaped -> render as literals.
        # A parser failure on the result still triggers the plain-text fallback.
        RESERVED = set("\\_*[]()~`>#+-=|{}.!")
        esc = lambda s: "".join("\\" + c if c in RESERVED else c for c in s)
        TOKEN = re.compile(r"`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|_[^_\n]+_")
        out, pos = [], 0
        for m in TOKEN.finditer(text):
            out.append(esc(text[pos:m.start()]))  # plain gap before this token
            tok = m.group(0)
            if tok[0] == "`":
                out.append(tok)  # code span: content is literal, markers intact
            elif tok[:2] == "**":  # MarkdownV2 has no ** -> collapse to single marker
                out.append(tok[0] + esc(tok[2:len(tok) - 2]) + tok[0])
            else:
                out.append(tok[0] + esc(tok[1:-1]) + tok[-1])  # keep markers, escape payload
            pos = m.end()
        out.append(esc(text[pos:]))
        return "".join(out)
    @staticmethod
    def _html(text):
        # convert the agent's markdown to Telegram HTML (the only rich tags are
        # <b> <i> <code> <pre>). Escape & < > first, then map `code` -> <code>,
        # **bold** / *italic* -> <b>/<i>. Unbalanced markers stay literal.
        esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        TOKEN = re.compile(r"`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*")
        out, pos = [], 0
        for m in TOKEN.finditer(text):
            out.append(esc(text[pos:m.start()]))
            tok = m.group(0)
            if tok[0] == "`":
                out.append("<code>" + esc(tok[1:-1]) + "</code>")
            elif tok[:2] == "**":
                out.append("<b>" + esc(tok[2:-2]) + "</b>")
            else:
                out.append("<i>" + esc(tok[1:-1]) + "</i>")
            pos = m.end()
        out.append(esc(text[pos:]))
        return "".join(out)
    def send(self, chat_id, text):
        # auto-routing: markdown table -> sendRichMessage (real table,
        # Bot API 10.1, cfg 'telegram_rich' = off disables it); very long text
        # -> .md document (1:1 formatting). Plain flow stays as before.
        if self.rich == "auto" and self._has_table(text):
            self.send_rich(chat_id, self.md_to_rich(text))
            return
        if len(text) > 4000:
            self.send_md(chat_id, text)
            return
        self._send_plain(chat_id, text)
    def _send_plain(self, chat_id, text):
        for i in range(0, len(text), 4000):  # Telegram hard limit is 4096 chars
            chunk = text[i:i + 4000]
            # format per cfg 'telegram_format' (markdown -> MarkdownV2,
            # html -> HTML, text -> plain); fall back to plain text when the parser
            # rejects the chunk (unbalanced entities)
            if self.fmt == "html":
                r = self._api("sendMessage", chat_id=chat_id, text=self._html(chunk),
                              parse_mode="HTML").get("result")
            elif self.fmt == "text":
                r = self._api("sendMessage", chat_id=chat_id, text=chunk).get("result")
            else:
                r = self._api("sendMessage", chat_id=chat_id, text=self._mdv2(chunk),
                              parse_mode="MarkdownV2").get("result")
            if r is None and self.fmt != "text":
                self._api("sendMessage", chat_id=chat_id, text=chunk)
    def send_pre(self, chat_id, text):
        # preformatted (e.g. the emblem): <pre> block = true monospace, layout
        # intact, and only & < > need escaping (no markdown reserved chars)
        esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for i in range(0, len(text), 3980):  # room for the <pre> tags
            chunk = text[i:i + 3980]
            r = self._api("sendMessage", chat_id=chat_id, text="<pre>" + esc(chunk) + "</pre>",
                          parse_mode="HTML").get("result")
            if r is None:  # parser rejected it: fall back to plain text
                self._api("sendMessage", chat_id=chat_id, text=chunk)
    def typing(self, chat_id):
        self._api("sendChatAction", chat_id=chat_id, action="typing")
    # --- rich messages (Bot API 10.1) + document upload -----------------
    # RichText (verified live against the real bot, 2026-11): a String (plain),
    # an Array of RichText, or a typed object {'type': bold|italic|code|..., 'text': ...}.
    # The type 'text' is REJECTED ("Unsupported rich text type").
    @staticmethod
    def _rich_text(text):
        """Inline markdown -> RichText (array of typed runs). `code` -> code,
        **bold** -> bold, *italic* -> italic; plain runs stay plain strings.
        Unbalanced markers stay literal."""
        out, pos = [], 0
        for m in re.finditer(r"`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*", text):
            if m.start() > pos:
                out.append(text[pos:m.start()])  # plain run: bare string
            tok = m.group(0)
            if tok[0] == "`":
                out.append({"type": "code", "text": tok[1:-1]})
            elif tok[:2] == "**":
                out.append({"type": "bold", "text": tok[2:-2]})
            else:
                out.append({"type": "italic", "text": tok[1:-1]})
            pos = m.end()
        if pos < len(text):
            out.append(text[pos:])
        return out
    @staticmethod
    def _has_table(md):
        """True when the text contains a markdown table (pipe line + separator line)."""
        lines = md.splitlines()
        for i in range(len(lines) - 1):
            if "|" in lines[i] and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]) and "-" in lines[i + 1]:
                return True
        return False
    def md_to_rich(self, md_text):
        """Small markdown -> RichBlock parser (stdlib, re). Tables (consecutive
        |...| lines + |---| separator) become bordered+striped table blocks
        (first row = header); #/## -> heading (size 1/2); -/* lists -> list
        (items carry paragraph blocks); `code`/**bold**/*italic* -> RichText
        runs; everything else -> paragraph (no data loss)."""
        blocks, lines, i = [], md_text.splitlines(), 0
        while i < len(lines):
            ln = lines[i]
            if not ln.strip():
                i += 1
                continue
            if "|" in ln and i + 1 < len(lines) and \
                    re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]) and "-" in lines[i + 1]:
                # table: header row, separator, then body rows while pipes continue
                def cells(row):
                    return [c.strip() for c in row.strip().strip("|").split("|")]
                rows = [cells(ln)]
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    rows.append(cells(lines[i]))
                    i += 1
                ncols = max(len(r) for r in rows)
                cells2d = []
                for ri, row in enumerate(rows):
                    row = row + [""] * (ncols - len(row))  # ragged rows: pad
                    cells2d.append([{"text": self._rich_text(c), "is_header": ri == 0} for c in row])
                blocks.append({"type": "table", "cells": cells2d,
                               "is_bordered": True, "is_striped": True})
                continue
            m = re.match(r"^(#{1,2})\s+(.*)$", ln)
            if m:
                blocks.append({"type": "heading", "size": len(m.group(1)),
                               "text": self._rich_text(m.group(2))})
                i += 1
                continue
            if re.match(r"^\s*[-*]\s+", ln):
                items = []
                while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                    items.append({"blocks": [{"type": "paragraph",
                                              "text": self._rich_text(re.sub(r"^\s*[-*]\s+", "", lines[i]))}]})
                    i += 1
                blocks.append({"type": "list", "items": items})
                continue
            para = [ln]
            i += 1
            while i < len(lines) and lines[i].strip() and "|" not in lines[i] \
                    and not re.match(r"^(#{1,2})\s+|^\s*[-*]\s+", lines[i]):
                para.append(lines[i])
                i += 1
            blocks.append({"type": "paragraph", "text": self._rich_text("\n".join(para))})
        return blocks
    @staticmethod
    def _rich_flat(rt):
        """RichText -> plain string (for the send_rich fallback)."""
        if isinstance(rt, str):
            return rt
        if isinstance(rt, list):
            return "".join(TelegramBot._rich_flat(x) for x in rt)
        return TelegramBot._rich_flat(rt.get("text", "")) if isinstance(rt, dict) else ""
    def send_rich(self, chat_id, blocks):
        """sendRichMessage (Bot API 10.1): real tables/headings/lists inline.
        Fallback: on error_code (e.g. old client API version) -> plain text via
        _send_plain(), so no message is lost."""
        r = self._api("sendRichMessage", chat_id=chat_id, rich_message={"blocks": blocks})
        if r.get("result") is None:
            flat = []
            for b in blocks:
                if b.get("type") == "table":
                    for row in b.get("cells", []):
                        flat.append(" | ".join(self._rich_flat(c.get("text", "")) for c in row))
                elif b.get("type") == "list":
                    for it in b.get("items", []):
                        flat.append("- " + self._rich_flat(it.get("blocks", [{}])[0].get("text", "")))
                else:
                    flat.append(self._rich_flat(b.get("text", "")))
            # _send_plain (not send): the flattened text still contains pipes,
            # and send() would re-detect the table -> infinite fallback loop
            self._send_plain(chat_id, "\n".join(flat))
        return r
    def send_file(self, chat_id, path, caption=""):
        """sendDocument: the file as a chat document (1:1 formatting for long
        or exact content). multipart/form-data via stdlib; content type from
        mimetypes (generic: any file type works)."""
        path = str(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        boundary = "----necro" + os.urandom(8).hex()
        def field(name, value):
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    f"{value}\r\n").encode()
        with open(path, "rb") as f:
            data = f.read()
        body = b"".join([field("chat_id", str(chat_id))] +
                       ([field("caption", caption)] if caption else []) +
                       [f"--{boundary}\r\nContent-Disposition: form-data; "
                        f"name=\"document\"; filename=\"{os.path.basename(path)}\"\r\n"
                        f"Content-Type: {ctype}\r\n\r\n".encode(),
                        data, f"\r\n--{boundary}--\r\n".encode()])
        c = http.client.HTTPSConnection("api.telegram.org", timeout=60)
        try:
            c.request("POST", f"/bot{self.token}/sendDocument", body,
                      {"Content-Type": f"multipart/form-data; boundary={boundary}"})
            return json.loads(c.getresponse().read())
        finally:
            c.close()
    def send_md(self, chat_id, text):
        """Markdown text as a .md document (exact 1:1 formatting)."""
        fd, tmp = tempfile.mkstemp(suffix=".md", prefix="necro-tg-")
        try:
            os.write(fd, text.encode())
            os.close(fd)
            return self.send_file(chat_id, tmp)
        finally:
            os.unlink(tmp)
    def next(self):
        return self.q.pop(0) if self.q else None
