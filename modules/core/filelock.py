"""Process-wide file lock (fcntl.flock) - stdlib only, Linux.

Several NECRO instances on the same workspace (peer mechanism, manual
2nd instance) race on the same files: both read the old content, both write,
the second os.replace silently wins -> lost update. flock on a per-file LOCK
FILE serializes across processes AND within one process (each call opens its
own FD, and flock is per-FD) - so it is the single lock layer; the former
threading.Lock (io.tools._write_lock) was redundant and removed.

Why a lock file and not the data file itself: os.replace swaps the data file's
INODE, so a flock on the data file would sit on the now-unmounted inode after
the replace - the second instance would never see it. The lock file stays put
(stable inode). Verified 2026-07-09: flock works across the bind mounts here
(same underlying volume/inode), and releases automatically when the FD closes
(also on crash) - no stale-lock problem like PID files.
"""
import errno
import fcntl
import os
import time
from contextlib import contextmanager

POLL = 0.05  # s between non-blocking lock attempts (flock has no native timeout)


@contextmanager
def file_lock(path, timeout=10.0):
    """Exclusive lock on <path>.lock (created next to the data file).
    Blocking with timeout: polls LOCK_EX|LOCK_NB every POLL seconds.
    Raises TimeoutError('file busy: <path>') when not acquired in time.
    The lock releases on FD close - automatically at context exit AND on
    process crash (no cleanup needed, the 0-byte lock file may stay)."""
    lock_path = str(path) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    t0 = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                # only EAGAIN (lock held) retries; other errors (EMFILE, EACCES,
                # ENOLCK) are real failures - a retry loop would mask them as
                # a false "file busy" timeout (peer review, Session 70)
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() - t0 >= timeout:
                    raise TimeoutError(f"file busy: {path} (lock held > {timeout:.0f}s)")
                time.sleep(POLL)
        yield
    finally:
        os.close(fd)  # closing the FD releases the flock
