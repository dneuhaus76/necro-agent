"""core tool implementations (bash, grep, read_file, write_file, edit_file).
Pure functions without App state (the live-stream callback + stop event are passed
in) - App.run_tool stays a thin dispatcher. Unit-testable without a server."""
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from modules.ui.render import diff_view
BASH_TIMEOUT = 120  # s, wall-time cap of the bash tool (SSOT: run_bash default, tool description, timeout notes)
TOOL_OUT_CAP = 8000  # max chars of tool output sent to the model (bash + grep)
CONTENT_CAP = 20000  # max chars of file/context content sent to the model (SSOT: read_file,
                     # tool-result truncation in agent._run_one, MD context load in necro.py)
def cap_text(o, cap=TOOL_OUT_CAP):
    """Cut tool output to cap chars with a truncation marker (SSOT for bash + grep)."""
    return o[:cap] + "\n…[truncated]" if len(o) > cap else o
def run_bash(command, stop, on_line=None, timeout=BASH_TIMEOUT):
    """Run a shell command, streaming output lines via on_line (live display).
    the join runs in short slices so a stop flag kills a long-running command
    right away instead of blocking until the timeout. Returns
    (output, returncode, timed_out, aborted)."""
    p = subprocess.Popen(command, shell=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True)
    out_lines = []
    def pump():
        first = True
        try:
            for ln in p.stdout:
                out_lines.append(ln)
                if on_line is not None:
                    on_line(("\n" if first else "") + ln)  # separate the head line
                    first = False
        except (ValueError, OSError):
            pass  # pipe closed early (shell exited, a background grandchild kept it open)
    pt = threading.Thread(target=pump, daemon=True)
    pt.start()
    timed_out, t0 = False, time.monotonic()
    while pt.is_alive():
        if time.monotonic() - t0 >= timeout:
            timed_out = True
            break
        if stop.is_set():
            break
        if p.poll() is not None and not pt.join(0.5):
            # the shell exited but a background grandchild (e.g. `sleep 100 &`)
            # still holds the stdout pipe open -> the pump would wait for EOF until
            # BASH_TIMEOUT. Kill the process group: the shell is already gone,
            # only strays remain, and their death delivers the EOF. (p.stdout.close()
            # would deadlock here: the TextIOWrapper lock is held by the pump's
            # blocked read.) The join is bounded so a stray that moved to its own
            # session (setsid) falls through to the normal timeout/kill path below.
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            pt.join(max(0.5, timeout - (time.monotonic() - t0)))
            break
        pt.join(0.2)
    if pt.is_alive():
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()  # process died between timeout and killpg: no output lost
        p.stdout.close()  # unblock the pump
        pt.join()
    p.wait()
    return "".join(out_lines), p.returncode, timed_out, stop.is_set()
def run_grep(pattern, path="."):
    """rg (fallback grep -rnE), 30 s timeout, TOOL_OUT_CAP cap."""
    cmd = ["rg", "-n", "--max-count", "100", "--", pattern, path or "."]
    if not shutil.which("rg"):  # fallback for systems without ripgrep
        cmd = ["grep", "-rnE", "--", pattern, path or "."]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, errors="replace")
    o = cap_text(p.stdout.strip())
    return o or f"no matches (exit {p.returncode})"
def read_file(path, offset=1, limit=0):
    """Read a file with 1-based offset/limit; 20k-char cap with a continuation
    marker. the fast path (offset=1, no limit) cuts at a line boundary so the
    continuation offset points to the NEXT full line, not into a partial one."""
    p = Path(path).expanduser()
    off, lim = max(1, int(offset or 1)), max(0, int(limit or 0))
    if off == 1 and not lim:  # fast path: only the first CONTENT_CAP chars are shown anyway,
        with p.open(errors="replace") as f:  # so a huge file (log dump, venv)
            t = f.read(CONTENT_CAP)  # is not fully loaded into memory
        if len(t) >= CONTENT_CAP:
            i = t.rfind("\n")
            if i > 0:
                t, nxt = t[:i], t[:i].count("\n") + 2  # next FULL line after the cut
            else:
                nxt = 1  # no complete line in the first CONTENT_CAP chars
            t += f"\n…[truncated, {p.stat().st_size} bytes total - continue with offset {nxt}]"
        return t
    t = p.read_text(errors="replace")
    lines = t.split("\n")
    if off > len(lines):
        return f"error: offset {off} beyond EOF ({len(lines)} lines)"
    sel = lines[off - 1: off - 1 + lim if lim else None]
    body = "\n".join(sel)
    if len(body) > CONTENT_CAP:
        cut = body[:CONTENT_CAP]
        shown = cut.count("\n") + (not cut.endswith("\n"))
        body = cut + "\n…[truncated]"
    else:
        shown = len(sel)
    return f"[lines {off}-{off + shown - 1} of {len(lines)}]\n" + body
_WRITE_LOCKS = {}  # resolved path -> Lock; serializes read-modify-write per file
_WRITE_LOCKS_GUARD = threading.Lock()
def _write_lock(path):
    """Per-file lock: tools run in parallel (ThreadPoolExecutor), and two
    concurrent edits of the SAME file would otherwise race - both read the old
    content, both write, and one edit is silently lost (or the shared .tmp is
    moved by the first os.replace, so the second fails with FileNotFoundError)."""
    key = str(Path(path).expanduser().resolve())
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(key, threading.Lock())
def _atomic_write(p, text):
    """Write via a temp file + rename: a crash mid-write cannot corrupt the target.
    The tmp name is unique per thread (pid + thread id) - a leftover .tmp of a
    dead thread must not be mistaken for this write's temp file."""
    tmp = p.parent / f"{p.name}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        tmp.write_text(text)
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
def write_file(path, content):
    p = Path(path).expanduser()
    cur = p.parent  # a file component in the path (e.g. 'modules/necro.py/x') would become a dir via mkdir
    while cur != cur.parent:
        if cur.is_file():
            return f"error: {cur} is a file (not a directory)"
        cur = cur.parent
    p.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock(p):
        _atomic_write(p, content)
    return f"wrote {len(content)} chars to {p}"
def edit_file(path, old, new):
    if not old:
        return "error: old text must not be empty"
    p = Path(path).expanduser()
    if p.is_dir():
        return f"error: {p} is a directory"
    with _write_lock(p):  # read + count + write as one critical section (see _write_lock)
        s = p.read_text(errors="replace")
        if (k := s.count(old)) != 1:
            return f"error: old text found {k}x (exactly 1 required)"
        _atomic_write(p, s.replace(old, new))
    return diff_view(s, old, new)
