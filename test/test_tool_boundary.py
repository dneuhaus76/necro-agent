"""The tool boundary: every field of a tool call comes from the model.

A missing key, a dict instead of a JSON string, a truncated argument block - all
of it must become an error STRING (one tool result) and never an exception that
escapes and aborts the turn. Before the guard in Agent.run_tool a call like
{"name": "grep"} raised KeyError('args') straight through turn().
"""
import threading
from pathlib import Path

import pytest

from modules.core import agent as agent_mod
from modules.io import tools

class _App:
    """Minimal App: the tool functions touch stop, model, sessions and add()."""

    def __init__(self):
        self.stop = threading.Event()
        self.model = "main-model"
        self.blocks = []

    def add(self, kind, text="", color=None):
        self.blocks.append((kind, text))
        return type("B", (), {"text": text, "live": False})()

    def append(self, b, text):
        b.text += text

    def _note_cwd(self):
        pass

@pytest.fixture
def ag():
    return agent_mod.Agent(_App(), "http://127.0.0.1:9/v1", 5, [], [], [], {}, "", "")

def test_missing_name_is_an_error_string(ag):
    assert ag.run_tool({"args": "{}"}).startswith("error: malformed tool call")
    assert ag.run_tool({"name": "", "args": "{}"}).startswith("error: malformed tool call")
    assert ag.run_tool({"name": None}).startswith("error: malformed tool call")

def test_missing_args_key_is_an_error_string(ag):
    """The old code did json.loads(c["args"]) OUTSIDE the try -> KeyError."""
    assert ag.run_tool({"name": "grep"}) == "error: grep: missing 'pattern'"

def test_non_object_call_is_an_error_string(ag):
    assert ag.run_tool("grep").startswith("error: malformed tool call")

@pytest.mark.parametrize("raw", ["{bad", "[]", "0", '"text"'])
def test_bad_arguments_give_a_precise_message(ag, raw):
    out = ag.run_tool({"name": "grep", "args": raw})
    assert out.startswith("error: grep: ")

def test_arguments_may_arrive_as_a_dict(ag, tmp_path):
    """Some servers deliver the parsed object instead of a JSON string."""
    f = tmp_path / "a.txt"
    f.write_text("hit\n")
    out = ag.run_tool({"name": "grep", "args": {"pattern": "hit", "path": str(f)}})
    assert "hit" in out

@pytest.mark.parametrize("name,args,missing", [
    ("grep", {}, "pattern"),
    ("recall", {}, "query"),
    ("read_file", {}, "path"),
    ("write_file", {"path": "/tmp/x"}, "content"),
    ("edit_file", {"path": "/tmp/x", "old": "a"}, "new"),
    ("delegate", {}, "model"),
    ("consult", {"model": "m"}, "question"),
])
def test_required_arguments_are_named(ag, name, args, missing):
    out = ag.run_tool({"name": name, "args": args})
    assert out == f"error: {name}: missing '{missing}'"

def test_blank_string_counts_as_missing(ag):
    assert ag.run_tool({"name": "grep", "args": {"pattern": "   "}}) == "error: grep: missing 'pattern'"

def test_unknown_tool_still_reports_the_name(ag):
    assert ag.run_tool({"name": "nope", "args": "{}"}) == "error: unknown tool nope"

def test_tool_exception_becomes_a_result(ag, tmp_path):
    """A tool bug must cost one tool result, not the whole turn."""
    out = ag.run_tool({"name": "read_file", "args": {"path": str(tmp_path / "absent")}})
    assert out.startswith("error: ") and "Error" in out


# ---- read_file: bounded reads -------------------------------------------
def _big(path, lines=200_000):
    with open(path, "w") as f:
        for i in range(lines):
            f.write(f"line {i} " + "x" * 40 + "\n")

class _NoFullRead:
    """File wrapper: iteration passes through, an unbounded read() is recorded."""

    def __init__(self, fh):
        self._fh, self.sizes = fh, []

    def __iter__(self):
        return iter(self._fh)

    def read(self, size=-1):
        self.sizes.append(size)
        return self._fh.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._fh.close()

def _spy_open(monkeypatch, tmp_path, calls):
    real_open = Path.open

    def fake_open(self, *a, **kw):
        fh = _NoFullRead(real_open(self, *a, **kw))
        calls.append(fh)
        return fh

    monkeypatch.setattr(Path, "open", fake_open)

def test_line_range_does_not_read_the_whole_file(tmp_path, monkeypatch):
    """offset/limit come from the model: a huge file must stay cheap. The old path
    did p.read_text() (the whole file) and cut afterwards - 611 MB RSS for a 36-char
    answer on a 300 MB file."""
    f = tmp_path / "big.log"
    _big(f)
    calls = []
    _spy_open(monkeypatch, tmp_path, calls)
    out = tools.read_file(str(f), offset=2, limit=1)
    assert out.startswith("[lines 2-2+, more follows]") and "line 1 " in out
    assert calls, "read_file did not open the file"
    assert all(size >= 0 for fh in calls for size in fh.sizes), "an unbounded read() happened"

def test_fast_path_reads_bounded(tmp_path, monkeypatch):
    f = tmp_path / "big.log"
    _big(f)
    calls = []
    _spy_open(monkeypatch, tmp_path, calls)
    tools.read_file(str(f))
    assert calls and calls[0].sizes == [tools.CONTENT_CAP]

def test_limit_is_capped_by_the_char_budget(tmp_path):
    f = tmp_path / "big.log"
    _big(f, lines=5_000)
    out = tools.read_file(str(f), limit=10 ** 9)  # a limit the model may well invent
    assert len(out) <= tools.CONTENT_CAP + 100
    assert "more follows" in out.splitlines()[0]

def test_offset_beyond_eof_is_a_precise_error(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("a\nb\nc\n")
    assert tools.read_file(str(f), offset=4) == "error: offset 4 beyond EOF"
    assert tools.read_file(str(f), offset=3, limit=9) == "[lines 3-3]\nc"
    assert tools.read_file(str(f), offset=1, limit=2) == "[lines 1-2+, more follows]\na\nb"

def test_fast_path_reports_the_continuation_line(tmp_path):
    f = tmp_path / "long.txt"
    f.write_text("\n".join("y" * 50 for _ in range(2000)))
    out = tools.read_file(str(f))
    assert "continue with offset" in out
    assert len(out) < tools.CONTENT_CAP + 200


# ---- atomic_write: one implementation ----------------------------------
def test_atomic_write_sets_the_requested_mode(tmp_path):
    p = tmp_path / "secret.json"
    tools.atomic_write(p, "{}", mode=0o600)
    assert (p.stat().st_mode & 0o777) == 0o600
    assert p.read_text() == "{}"
    assert not [x for x in tmp_path.iterdir() if x.name != "secret.json"]  # no tmp left

def test_atomic_write_keeps_the_mode_of_an_existing_file(tmp_path):
    p = tmp_path / "run.sh"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    tools.atomic_write(p, "#!/bin/sh\necho ok\n")
    assert (p.stat().st_mode & 0o777) == 0o755
    assert "echo ok" in p.read_text()
