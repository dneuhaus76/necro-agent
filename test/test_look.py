"""look tool: image file -> (text line, base64, mime) plus the one-shot
injection into the next request.

The tool result is a plain string (path, type, size). The image rides in an
extra user message with an image_url part for exactly ONE request and is then
replaced by a text placeholder - so no content list ever reaches turn.py,
context.py or sessiondb.py.
"""
import base64
import threading

import pytest

from modules.core.agent import Agent
from modules.io import tools

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 60
GIF = b"GIF89a" + b"\x00" * 60


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_look_jpeg(tmp_path):
    p = _write(tmp_path, "a.jpg", JPEG)
    text, b64, mime = tools.run_look(p)
    assert mime == "image/jpeg"
    assert text.startswith(f"image: {p} (jpeg, ")
    assert base64.b64decode(b64) == JPEG  # the bytes come back unchanged


def test_look_png(tmp_path):
    p = _write(tmp_path, "a.png", PNG)
    text, b64, mime = tools.run_look(p)
    assert mime == "image/png"
    assert "(png, " in text
    assert base64.b64decode(b64) == PNG


def test_look_webp(tmp_path):
    p = _write(tmp_path, "a.webp", WEBP)
    text, b64, mime = tools.run_look(p)
    assert mime == "image/webp"
    assert "(webp, " in text
    assert base64.b64decode(b64) == WEBP


def test_look_gif_rejected(tmp_path):
    p = _write(tmp_path, "a.gif", GIF)
    text, b64, mime = tools.run_look(p)
    assert text.startswith("error: look:") and "not JPEG, PNG or WebP" in text
    assert b64 is None and mime is None


def test_look_missing(tmp_path):
    text, b64, mime = tools.run_look(str(tmp_path / "nope.png"))
    assert text.startswith("error: look: no such file")
    assert b64 is None and mime is None


def test_look_too_large(tmp_path):
    p = tmp_path / "big.png"
    p.write_bytes(PNG)
    with p.open("r+b") as f:
        f.truncate(tools.LOOK_MAX_BYTES + 1)  # sparse: no 8 MB write
    text, b64, mime = tools.run_look(str(p))
    assert text.startswith("error: look:") and "limit is 8 MB" in text
    assert b64 is None and mime is None


def test_look_directory(tmp_path):
    text, b64, mime = tools.run_look(str(tmp_path))
    assert "is a directory" in text
    assert b64 is None and mime is None


def test_look_empty(tmp_path):
    p = _write(tmp_path, "empty.png", b"")
    text, b64, mime = tools.run_look(p)
    assert "is empty" in text
    assert b64 is None and mime is None


# ---- one-shot injection (no server needed: the helpers are pure list moves)
def _agent():
    app = type("A", (), {"messages": [{"role": "system", "content": "sys"}],
                         "stop": threading.Event(), "model": "m"})()
    return Agent(app, "http://local", 5, [], [], [], {}, "", "")


def test_look_tool_result_is_a_plain_string(tmp_path):
    """The tool result stays a string - only the user message carries the image."""
    p = _write(tmp_path, "a.png", PNG)
    ag = _agent()
    res = ag._tool_look({"path": p}, None)
    assert isinstance(res, str) and res.startswith("image: ")
    assert ag._pending_images and ag._pending_images[0]["url"].startswith("data:image/png;base64,")


def test_look_missing_path_arg():
    ag = _agent()
    assert ag._tool_look({}, None) == "error: look: missing 'path'"
    assert ag._pending_images == []


def test_images_are_one_shot(tmp_path):
    """push -> the image is in the request; settle -> a text placeholder remains."""
    p = _write(tmp_path, "a.png", PNG)
    ag = _agent()
    ag._tool_look({"path": p}, None)
    msgs = ag._push_images()
    assert len(msgs) == 1
    last = ag.app.messages[-1]
    assert last["role"] == "user" and isinstance(last["content"], list)
    assert last["content"][0]["text"] == f"[image: {p}]"
    assert last["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    ag._settle_images(msgs)  # the request is over
    assert ag.app.messages[-1] == {"role": "user", "content": f"[image: {p}]"}
    assert ag._pending_images == []  # nothing left for the next request


def test_two_images_in_one_step_both_survive(tmp_path):
    """Parallel look calls must not overwrite each other (list, not one slot)."""
    ag = _agent()
    a = _write(tmp_path, "a.png", PNG)
    b = _write(tmp_path, "b.png", PNG)
    ag._tool_look({"path": a}, None)
    ag._tool_look({"path": b}, None)
    assert len(ag._push_images()) == 2


def test_settle_survives_a_rebuilt_message_list(tmp_path):
    """compact() may replace app.messages: a missing image message is no error."""
    p = _write(tmp_path, "a.png", PNG)
    ag = _agent()
    ag._tool_look({"path": p}, None)
    msgs = ag._push_images()
    ag.app.messages = [{"role": "system", "content": "sys"}]
    ag._settle_images(msgs)  # must not raise
    assert len(ag.app.messages) == 1


def test_pending_image_does_not_leak_into_the_next_turn(tmp_path):
    """look in the last step, turn ends -> the image must not ride in a later turn."""
    p = _write(tmp_path, "a.png", PNG)
    ag = _agent()
    ag._tool_look({"path": p}, None)
    ag.reset_counters()  # App.turn calls this before the next turn
    assert ag._pending_images == []
    assert ag._push_images() == []
