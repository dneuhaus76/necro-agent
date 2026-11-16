"""two parallel tool calls editing the SAME file must not race.
Before the fix, _atomic_write used one shared .tmp per target file: the first
os.replace() moved it, the second failed with FileNotFoundError (and without the
per-file lock, both edits read the same old content -> one edit silently lost)."""
import threading

from modules.io import tools


def test_concurrent_edits_same_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("line1\nline2\nline3\n")
    errors = []

    def edit(old, new):
        try:
            r = tools.edit_file(str(p), old, new)
            if r.startswith("error"):
                errors.append(r)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=edit, args=("line1", "LINE1")),
          threading.Thread(target=edit, args=("line2", "LINE2")),
          threading.Thread(target=edit, args=("line3", "LINE3"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert not errors, errors
    # all three edits applied (no lost update)
    assert p.read_text() == "LINE1\nLINE2\nLINE3\n"


def test_concurrent_write_and_edit_same_file(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("alpha\nbeta\n")
    errors = []

    def w():
        try:
            r = tools.write_file(str(p), "gamma\ndelta\n")
            if r.startswith("error"):
                errors.append(r)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    def e():
        try:
            r = tools.edit_file(str(p), "beta", "BETA")
            # 'old text found 0x' is a legitimate outcome: the write won the
            # race and replaced the content before this edit read it
            if r.startswith("error") and "found 0x" not in r:
                errors.append(r)
        except Exception as ex:
            errors.append(f"{type(ex).__name__}: {ex}")

    ts = [threading.Thread(target=w), threading.Thread(target=e)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert not errors, errors
    # exactly one outcome, both consistent (no torn state)
    content = p.read_text()
    assert content in ("gamma\ndelta\n", "alpha\nBETA\n")


def test_no_leftover_tmp(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x\n")
    tools.edit_file(str(p), "x", "y")
    tools.write_file(str(p), "z\n")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["f.txt", "f.txt.lock"]  # the flock file stays (0 bytes)
