"""core tool implementations (bash, grep, read_file, write_file, edit_file).
Pure functions without App state (the live-stream callback + stop event are passed
in) - App.run_tool stays a thin dispatcher. Unit-testable without a server."""
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from modules.core.filelock import file_lock
from modules.ui.render import diff_view
BASH_TIMEOUT = 120  # s, wall-time cap of the bash tool (SSOT: run_bash default, tool description, timeout notes)
TOOL_OUT_CAP = 8000  # max chars of tool output sent to the model (bash + grep)
CONTENT_CAP = 20000  # max chars of file/context content sent to the model (SSOT: read_file,
                     # tool-result truncation in agent._run_one, MD context load in necro.py)
GREP_MAX_COUNT = 100  # max matches per file for the grep tool (SSOT: run_grep, tool description)
GREP_TIMEOUT = 30  # s, wall-time cap of the grep tool (was a hard-coded 30 in subprocess.run)
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
def stop_hit(stop):
    """Was an abort requested? stop is optional (None = no abort channel)."""
    return stop is not None and stop.is_set()

def wait_slices(p, stop, timeout):
    """Wait for p while polling stop in short slices (the run_bash pattern), so an
    abort returns at once instead of blocking until the wall-time cap. Returns
    'done' | 'timeout' | 'aborted'; the caller kills the process group."""
    t0 = time.monotonic()
    while p.poll() is None:
        if stop_hit(stop):
            return "aborted"
        if time.monotonic() - t0 >= timeout:
            return "timeout"
        time.sleep(0.02)
    return "done"

def kill_group(p):
    """Kill the child's whole process group (start_new_session=True) - a shell
    wrapper would otherwise survive its children and keep the pipe open."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        try:
            p.kill()
        except OSError:
            pass  # already dead

def run_grep(pattern, path=".", stop=None):
    """rg (fallback grep -rnE), 30 s timeout, TOOL_OUT_CAP cap. The wait polls stop
    in short slices (wait_slices), so an abort does not hang until GREP_TIMEOUT."""
    if not pattern:  # guard: an empty pattern matches every line of every file
        return "error: grep: missing 'pattern'"
    cmd = ["rg", "-n", "--max-count", str(GREP_MAX_COUNT), "--", pattern, path or "."]
    if not shutil.which("rg"):  # fallback for systems without ripgrep
        cmd = ["grep", "-rnE", "--", pattern, path or "."]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, errors="replace",
                         start_new_session=True)
    out = []
    def pump():
        try:
            out.extend(p.stdout)
        except (ValueError, OSError):
            pass  # pipe closed early (killed on abort)
    pt = threading.Thread(target=pump, daemon=True)
    pt.start()
    state = wait_slices(p, stop, GREP_TIMEOUT)
    if state != "done":
        kill_group(p)
        p.stdout.close()  # unblock the pump
    pt.join(1)  # bounded: a stuck reader must not hang the tool past the abort
    p.wait()
    o = cap_text("".join(out).strip())
    if state == "aborted":
        return f"{o}\n[aborted]".strip() if o else "[aborted]"
    if state == "timeout":
        return f"{o}\n[timeout after {GREP_TIMEOUT} s]".strip() if o else f"[timeout after {GREP_TIMEOUT} s]"
    return o or f"no matches (exit {p.returncode})"
def read_file(path, offset=1, limit=0, stop=None):
    """Read a file with 1-based offset/limit; 20k-char cap with a continuation
    marker. the fast path (offset=1, no limit) cuts at a line boundary so the
    continuation offset points to the NEXT full line, not into a partial one.
    The read itself is capped, so a stop check at entry is enough (a chunked
    reader would be a rewrite without a latency win)."""
    if stop_hit(stop):
        return "[aborted]"
    p = Path(path).expanduser()
    off, lim = max(1, int(offset or 1)), max(0, int(limit or 0))
    if off == 1 and not lim:  # fast path: only the first CONTENT_CAP chars are shown anyway,
        with p.open(errors="replace") as f:  # so a huge file (log dump, venv)
            t = f.read(CONTENT_CAP)  # is not fully loaded into memory
        if len(t) >= CONTENT_CAP:
            # only a file with MORE content than the cap is truncated: a file of
            # exactly CONTENT_CAP chars would be reported truncated with
            # 'continue with offset 1' - an endless re-read of the same content
            total = p.stat().st_size
            if total > len(t):
                i = t.rfind("\n")
                if i > 0:
                    t, nxt = t[:i], t[:i].count("\n") + 2  # next FULL line after the cut
                else:
                    nxt = 1  # no complete line in the first CONTENT_CAP chars
                t += f"\n…[truncated, {total} bytes total - continue with offset {nxt}]"
        return t
    t = p.read_text(errors="replace")
    if stop_hit(stop):  # a large file may take a moment - the turn was aborted meanwhile
        return "[aborted]"
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
def _atomic_write(p, text):
    """Write via a temp file + rename: a crash mid-write cannot corrupt the target.
    The tmp name is unique per thread (pid + thread id) - a leftover .tmp of a
    dead thread must not be mistaken for this write's temp file."""
    tmp = p.parent / f"{p.name}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        tmp.write_text(text)
        if p.exists():  # preserve existing mode (e.g. u+x) - a fresh tmp file would reset it to umask 644
            os.chmod(tmp, stat.S_IMODE(p.stat().st_mode))
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
def write_file(path, content, stop=None):
    if stop_hit(stop):
        return "[aborted]"
    p = Path(path).expanduser()
    cur = p.parent  # a file component in the path (e.g. 'modules/necro.py/x') would become a dir via mkdir
    while cur != cur.parent:
        if cur.is_file():
            return f"error: {cur} is a file (not a directory)"
        cur = cur.parent
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(p):  # flock serializes inter- AND intra-process; one lock layer
        _atomic_write(p, content)
    return f"wrote {len(content)} chars to {p}"
def edit_file(path, old, new):
    if not old:
        return "error: old text must not be empty"
    p = Path(path).expanduser()
    if p.is_dir():
        return f"error: {p} is a directory"
    with file_lock(p):  # flock serializes inter- AND intra-process; read+count+write as one critical section
        s = p.read_text(errors="replace")
        if (k := s.count(old)) != 1:
            return f"error: old text found {k}x (exactly 1 required)"
        _atomic_write(p, s.replace(old, new))
    return diff_view(s, old, new)
