"""Telegram client for NECRO (long polling, stdlib only)."""
import http.client, json, re, threading, time
class TelegramBot:
    """Long-polling Telegram client (pi-telegram-plus-style): incoming text messages
    become agent turns, the final agent reply is sent back. Stdlib only."""
    def __init__(self, token, fmt="markdown", whitelist=()):
        # fmt = 'markdown' (default, MarkdownV2) | 'html' | 'text'
        # whitelist = chat ids that may spend tokens; messages from other chats
        # go to stranger_q instead of the prompt queue. Empty = everyone.
        self.token, self.seen, self.seen_order, self.q, self.fmt = token, set(), [], [], fmt
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
    def next(self):
        return self.q.pop(0) if self.q else None
