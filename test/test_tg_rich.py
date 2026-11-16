"""markdown -> RichBlock parser (sendRichMessage, Bot API 10.1) +
send() auto-routing (table -> rich, long text -> .md document) + fallback.
RichText structure verified live against the real bot (2026-11): a String,
an Array of RichText, or a typed object {'type': bold|italic|code, 'text': ...}."""
import json

from modules.io.tg import TelegramBot


def bot():
    return TelegramBot.__new__(TelegramBot)  # no polling thread in unit tests


def txt(rich):
    """Concatenate a RichText (str | list | typed object) to plain text."""
    return TelegramBot._rich_flat(rich)


def test_md_to_rich_table():
    b = bot()
    blocks = b.md_to_rich("| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |")
    assert len(blocks) == 1
    t = blocks[0]
    assert t["type"] == "table" and t["is_bordered"] and t["is_striped"]
    assert len(t["cells"]) == 3  # header + 2 body rows
    assert all(c["is_header"] for c in t["cells"][0])
    assert not any(c["is_header"] for row in t["cells"][1:] for c in row)
    assert txt(t["cells"][0][0]["text"]) == "a"
    assert txt(t["cells"][2][1]["text"]) == "4"


def test_md_to_rich_heading_list_paragraph():
    b = bot()
    blocks = b.md_to_rich("# Title\n\n- one\n- two\n\nplain **bold** text")
    assert [x["type"] for x in blocks] == ["heading", "list", "paragraph"]
    assert blocks[0]["size"] == 1
    assert txt(blocks[0]["text"]) == "Title"
    assert len(blocks[1]["items"]) == 2
    assert txt(blocks[1]["items"][1]["blocks"][0]["text"]) == "two"
    # inline formatting: bold run is a typed object, plain runs stay strings
    para = blocks[2]["text"]
    assert any(isinstance(x, dict) and x.get("type") == "bold" and x.get("text") == "bold" for x in para)


def test_md_to_rich_rich_text_types():
    b = bot()
    rt = b._rich_text("**fett** *kursiv* `code`")
    # the space between tokens is a real (plain) string run, so check membership
    assert {"type": "bold", "text": "fett"} in rt
    assert {"type": "italic", "text": "kursiv"} in rt
    assert {"type": "code", "text": "code"} in rt
    # the API rejects the type 'text' (verified live) - never emit it
    for x in b._rich_text("plain"):
        if isinstance(x, dict):
            assert x.get("type") != "text"


def test_md_to_rich_ragged_table_padded():
    b = bot()
    blocks = b.md_to_rich("| a | b |\n|---|---|\n| 1 |")
    t = blocks[0]
    assert len(t["cells"][1]) == 2  # padded to header width
    assert txt(t["cells"][1][1]["text"]) == ""


def test_md_to_rich_unknown_stays_paragraph():
    b = bot()
    blocks = b.md_to_rich("just a line\nsecond line")
    assert len(blocks) == 1 and blocks[0]["type"] == "paragraph"
    assert txt(blocks[0]["text"]) == "just a line\nsecond line"


def test_has_table():
    b = bot()
    assert b._has_table("a\n| x | y |\n|---|---|\n| 1 | 2 |")
    assert not b._has_table("a | b\nno separator here")
    assert not b._has_table("plain text")


def test_send_routes_table_to_rich():
    b = bot()
    b.token, b.fmt, b.rich = "t", "markdown", "auto"
    calls = []
    b._api = lambda method, **p: calls.append((method, p)) or {"result": True}
    b.send(1, "| a |\n|---|\n| 1 |")
    assert len(calls) == 1 and calls[0][0] == "sendRichMessage"
    assert calls[0][1]["rich_message"]["blocks"][0]["type"] == "table"


def test_send_rich_fallback_on_error_code():
    b = bot()
    b.token, b.fmt, b.rich = "t", "text", "auto"
    calls = []
    b._api = lambda method, **p: (calls.append(method), {"error_code": 400})[1]
    b.send_rich(1, [{"type": "paragraph", "text": [{"type": "bold", "text": "hi"}]}])
    assert calls == ["sendRichMessage", "sendMessage"]  # fallback via _send_plain()


def test_send_long_text_to_document():
    b = bot()
    b.token, b.fmt, b.rich = "t", "markdown", "auto"
    calls = []
    b.send_md = lambda cid, text: calls.append(("md", cid, len(text))) or {"result": True}
    b.send(1, "x" * 5000)
    assert calls == [("md", 1, 5000)]


def test_send_short_text_plain():
    b = bot()
    b.token, b.fmt, b.rich = "t", "text", "auto"
    calls = []
    b._api = lambda method, **p: calls.append(method) or {"result": True}
    b.send(1, "hello")
    assert calls == ["sendMessage"]


def test_rich_off_disables_table_detection():
    b = bot()
    b.token, b.fmt, b.rich = "t", "text", "off"
    calls = []
    b._api = lambda method, **p: calls.append(method) or {"result": True}
    b.send(1, "| a |\n|---|\n| 1 |")
    assert calls == ["sendMessage"]  # no sendRichMessage


def test_send_file_multipart(tmp_path):
    b = bot()
    b.token = "t"
    f = tmp_path / "doc.md"
    f.write_text("# hi")
    captured = {}

    class FakeConn:
        def request(self, method, path, body, headers):
            captured["path"], captured["body"], captured["headers"] = path, body, headers

        class _Resp:
            def read(self):
                return json.dumps({"ok": True, "result": True}).encode()

        def getresponse(self):
            return FakeConn._Resp()

        def close(self):
            pass

    import http.client
    orig = http.client.HTTPSConnection
    http.client.HTTPSConnection = lambda *a, **k: FakeConn()
    try:
        r = b.send_file(1, str(f), caption="cap")
    finally:
        http.client.HTTPSConnection = orig
    assert r["result"] is True
    assert captured["path"] == "/bott/sendDocument"
    body = captured["body"]
    assert b'name="chat_id"' in body and b"1\r\n" in body
    assert b'name="caption"' in body and b"cap" in body
    assert b'filename="doc.md"' in body and b"# hi" in body
    assert "multipart/form-data" in captured["headers"]["Content-Type"]
