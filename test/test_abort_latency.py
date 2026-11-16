"""abort latency - a stop flag must cut the tool wait at once (not after the
30 s grep cap) and the main loop must poll keys faster while a turn is running."""
import subprocess
import threading
import time

import pytest

from modules.io import tools


@pytest.fixture
def stop_event():
    return threading.Event()


def test_wait_slices_aborts_mid_run():
    """A stop set 50 ms into a long-running child returns at once, not after 30 s."""
    p = subprocess.Popen(["sleep", "5"], stdout=subprocess.DEVNULL)
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    t0 = time.monotonic()
    assert tools.wait_slices(p, stop, 30) == "aborted"
    tools.kill_group(p)
    p.wait()
    assert time.monotonic() - t0 < 1.0


def test_wait_slices_timeout_and_done():
    p = subprocess.Popen(["sleep", "5"], stdout=subprocess.DEVNULL)
    try:
        assert tools.wait_slices(p, None, 0.1) == "timeout"  # stop=None: no abort channel
    finally:
        tools.kill_group(p)
        p.wait()
    assert tools.wait_slices(subprocess.Popen(["true"]), None, 5) == "done"


def test_wait_slices_pre_set_stop(stop_event):
    """An already-aborted turn must not start a long child at all."""
    stop_event.set()
    p = subprocess.Popen(["sleep", "5"], stdout=subprocess.DEVNULL)
    try:
        t0 = time.monotonic()
        assert tools.wait_slices(p, stop_event, 30) == "aborted"
        assert time.monotonic() - t0 < 1.0
    finally:
        tools.kill_group(p)
        p.wait()


def test_run_grep_marks_abort(tmp_path, stop_event):
    (tmp_path / "a.txt").write_text("needle\n")
    stop_event.set()
    out = tools.run_grep("needle", str(tmp_path), stop_event)
    assert "[aborted]" in out


def test_run_grep_normal_and_guard(tmp_path):
    (tmp_path / "a.txt").write_text("needle\nother\n")
    out = tools.run_grep("needle", str(tmp_path))
    assert "a.txt:1:needle" in out
    assert tools.run_grep("", str(tmp_path)).startswith("error:")  # empty pattern = every line


def test_run_grep_stop_mid_run(stop_event, tmp_path):
    """Mid-run stop: the return happens well inside the 30 s grep cap."""
    for i in range(20):  # a small tree so the scan cost is dominated by the loop, not by I/O
        (tmp_path / f"f{i}.txt").write_text("x\n" * 1000)
    threading.Timer(0.05, stop_event.set).start()
    t0 = time.monotonic()
    tools.run_grep("needle-not-there", str(tmp_path), stop_event)
    assert time.monotonic() - t0 < 1.0


def test_read_write_abort_at_entry(tmp_path, stop_event):
    f = tmp_path / "x.txt"
    f.write_text("content")
    stop_event.set()
    assert tools.read_file(str(f), stop=stop_event) == "[aborted]"
    assert tools.write_file(str(f), "new", stop_event) == "[aborted]"
    assert f.read_text() == "content"  # untouched


def test_poll_timeout_states():
    """Main loop slice: busy 50 ms, idle 250 ms, pending lone ESC 10 ms."""
    import necro

    class App:
        rx, busy = "", False

    a = App()
    assert necro.poll_timeout(a) == 0.25
    a.busy = True
    assert necro.poll_timeout(a) == 0.05  # abort reacts within 50 ms while a turn runs
    a.rx = "\x1b"
    assert necro.poll_timeout(a) == 0.01
