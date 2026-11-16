"""process-wide file lock (fcntl.flock) - two PROCESSES doing
read-modify-write on the same file must not lose updates (the N204 race,
but across processes). Also: timeout, crash-release, lock file location."""
import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path

from modules.core.filelock import file_lock
from modules.io import tools


def _worker(path, n, barrier, result_q):
    """n read-modify-write increments on the same file, serialized by file_lock."""
    barrier.wait()
    for _ in range(n):
        with file_lock(path):
            try:
                cur = int(open(path).read() or 0)
            except FileNotFoundError:
                cur = 0
            with open(path + ".tmp", "w") as f:
                f.write(str(cur + 1))
            os.replace(path + ".tmp", path)
    result_q.put("done")


def test_two_processes_no_lost_update(tmp_path):
    p = str(tmp_path / "counter")
    open(p, "w").write("0")
    N = 25
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    q = ctx.Queue()
    ps = [ctx.Process(target=_worker, args=(p, N, barrier, q)) for _ in range(2)]
    for pr in ps:
        pr.start()
    for pr in ps:
        pr.join(30)
        assert pr.exitcode == 0
    assert q.get() == "done" and q.get() == "done"
    # without the lock, lost updates are likely (25x25 read-modify-writes);
    # with the lock the counter is exact
    assert int(open(p).read()) == 2 * N


def test_intra_process_threads_serialized(tmp_path):
    """flock ALONE serializes within ONE process: each call opens its own FD and
    flock is per-FD, so threads block on each other. This is the basis for
    removing the redundant threading.Lock (io.tools._write_lock, N204)."""
    p = str(tmp_path / "counter")
    open(p, "w").write("0")
    N = 5
    barrier = threading.Barrier(4)
    def worker():
        barrier.wait()
        for _ in range(N):
            with file_lock(p):
                cur = int(open(p).read() or 0)
                with open(p + ".tmp", "w") as f:
                    f.write(str(cur + 1))
                os.replace(p + ".tmp", p)
    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
        assert not t.is_alive()
    assert int(open(p).read()) == 4 * N


def test_timeout(tmp_path):
    p = str(tmp_path / "f.txt")
    open(p, "w").write("x")
    with file_lock(p):
        try:
            file_lock(p, timeout=0.3).__enter__()
            raise AssertionError("expected TimeoutError")
        except TimeoutError as e:
            assert "file busy" in str(e)


def test_lock_released_after_crash(tmp_path):
    p = str(tmp_path / "f.txt")
    open(p, "w").write("x")
    marker = tmp_path / "locked"
    ctx = multiprocessing.get_context("fork")

    def holder():
        with file_lock(p):
            marker.write_text("1")  # parent sees this only after the lock is held
            os.kill(os.getpid(), signal.SIGKILL)  # die while holding the lock

    pr = ctx.Process(target=holder)
    pr.start()
    for _ in range(200):  # wait for the marker (queue/pipe are unreliable across SIGKILL)
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()
    pr.join(10)
    assert pr.exitcode == -signal.SIGKILL.value  # killed by SIGKILL (negative = signal)
    # the FD closed with the process -> the flock is gone, we can lock now
    with file_lock(p, timeout=2):
        pass


def test_non_eagain_error_raises_immediately(tmp_path, monkeypatch):
    """EMFILE/EACCES/ENOLCK are real failures, not 'lock held' - they must not
    be retried into a false 'file busy' timeout (peer review, Session 70)."""
    import errno
    import fcntl as _fcntl

    p = str(tmp_path / "f.txt")
    open(p, "w").write("x")

    def fake_flock(fd, op):
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(_fcntl, "flock", fake_flock)
    t0 = time.monotonic()
    try:
        file_lock(p, timeout=5).__enter__()
        raise AssertionError("expected OSError")
    except TimeoutError:
        raise AssertionError("EMFILE must not be masked as a timeout")
    except OSError as e:
        assert e.errno == errno.EMFILE
    assert time.monotonic() - t0 < 1  # raised on the first attempt, no retry loop


def test_tools_use_lock_file(tmp_path):
    p = tmp_path / "f.txt"
    tools.write_file(str(p), "a\n")
    tools.edit_file(str(p), "a", "b")
    assert (tmp_path / "f.txt.lock").exists()  # lock file lives next to the data file
    assert (tmp_path / "f.txt.lock").stat().st_size == 0  # stays empty, no cleanup race


def test_bind_mount_lock():
    """flock must span the bind mounts: a file created via /tmp/necro is the
    SAME inode via /workspace/necro (verified 2026-07-09: same st_dev).
    Locking through one mount must block a lock through the other.
    Skipped when the test env lacks the mounts or they are different volumes.
    NOTE (Session 70): in the current container /tmp/necro and /workspace/necro
    are SEPARATE directories on the same ext4 volume (same st_dev, different
    inodes) - not bind mounts of each other - so the probe below skips."""
    a, b = Path("/tmp/necro"), Path("/workspace/necro")
    if not (a.is_dir() and b.is_dir()):
        return  # skip
    if a.stat().st_dev != b.stat().st_dev:  # different underlying devices -> flock cannot span them
        return
    f = a / "n214_bindlock_test"
    f.write_text("x")
    try:
        if not (b / "n214_bindlock_test").exists():
            return  # same volume but separate dirs, not a bind mount of each other
        with file_lock(f):
            try:
                file_lock(b / "n214_bindlock_test", timeout=0.3).__enter__()
                raise AssertionError("expected TimeoutError (same inode across mounts)")
            except TimeoutError:
                pass
    finally:
        f.unlink(missing_ok=True)
