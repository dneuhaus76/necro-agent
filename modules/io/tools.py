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
    """Read a file with 1-based offset/limit; CONTENT_CAP chars with a continuation
    marker. Both paths read BOUNDED: only the requested lines (plus one probe line)
    are touched, so a 2 GB log file or a venv cannot exhaust memory. The model
    chooses offset/limit, i.e. these are untrusted values."""
    if stop_hit(stop):
        return "[aborted]"
    p = Path(path).expanduser()
    off, lim = max(1, int(offset or 1)), max(0, int(limit or 0))
    with p.open(errors="replace") as f:
        if off == 1 and not lim:
            t = f.read(CONTENT_CAP)  # bounded: a huge file is never fully loaded
            if len(t) < CONTENT_CAP:
                return t
            # only a file with MORE content is truncated: a file of exactly
            # CONTENT_CAP chars would otherwise be reported truncated with
            # 'continue with offset 1' - an endless re-read of the same content
            total = p.stat().st_size
            if total <= len(t):
                return t
            i = t.rfind("\n")
            if i > 0:
                t, nxt = t[:i], t[:i].count("\n") + 2  # next FULL line after the cut
            else:
                nxt = 1  # no complete line in the first CONTENT_CAP chars
            return t + f"\n…[truncated, {total} bytes total - continue with offset {nxt}]"
        sel, chars, more = [], 0, False
        for i, line in enumerate(f):
            if i < off - 1:
                continue
            if lim and i > off - 1 + lim - 1:  # one line past the request: "is there more?"
                more = True
                break
            chars += len(line)
            if chars > CONTENT_CAP:  # the char cap wins over an oversized limit
                break
            sel.append(line.rstrip("\n"))
    if stop_hit(stop):  # the lines are cheap, but the turn may have been aborted
        return "[aborted]"
    if not sel:
        return f"error: offset {off} beyond EOF"
    body = "\n".join(sel)
    end_line = off + len(sel) - 1
    head = f"[lines {off}-{end_line}" + ("+, more follows]" if more or chars > CONTENT_CAP else "]")
    return head + "\n" + body


def atomic_write(p, text, mode=None):
    """Write via a temp file + rename: a crash mid-write cannot corrupt the target.

    The tmp name is unique per thread (pid + thread id) - a leftover .tmp of a
    dead thread must not be mistaken for this write's temp file.
    mode: explicit permissions (e.g. 0o600 for a file that carries a secret);
    default = keep the mode of an existing target (a fresh tmp file would
    otherwise reset it to umask 644)."""
    tmp = p.parent / f"{p.name}.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        tmp.write_text(text)
        if mode is not None:
            os.chmod(tmp, mode)
        elif p.exists():  # preserve the mode (e.g. u+x)
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
        atomic_write(p, content)
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
        atomic_write(p, s.replace(old, new))
    return diff_view(s, old, new)


# ---- look: image into the model context -----------------------------------
# Supported: what a llama.cpp mmproj typically handles. Anything else is a clear
# error instead of a request the server cannot parse.
LOOK_MAX_BYTES = 8 * 1024 * 1024  # hard cap, checked BEFORE the read (no 200 MB read)
_MAGIC = ((b"\xff\xd8\xff", "image/jpeg", "jpeg"),
          (b"\x89PNG\r\n\x1a\n", "image/png", "png"))
def _image_kind(head):
    """(mime, short type) from the first bytes, or (None, None) when unsupported.
    WebP carries 'RIFF' at 0 and 'WEBP' at 8 - checked separately because the
    4-byte prefix alone is not unique."""
    for magic, mime, short in _MAGIC:
        if head.startswith(magic):
            return mime, short
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None, None
def run_look(path):
    """Read an image file and return (text_line, base64, mime).
    The text line is what the model and the UI see; the base64 is handed to the
    caller for the one-shot image part. On any problem: (error string, None, None),
    same 'error:' convention as the other tools."""
    import base64
    if not path:
        return "error: look: missing 'path'", None, None
    p = Path(path).expanduser()
    try:
        if p.is_dir():
            return f"error: look: {p} is a directory", None, None
        if not p.exists():
            return f"error: look: no such file: {p}", None, None
        if not p.is_file():
            return f"error: look: not a regular file: {p}", None, None
        size = p.stat().st_size  # size first: never read a huge file just to reject it
    except OSError as e:
        return f"error: look: {p}: {e.strerror or e}", None, None
    if size == 0:
        return f"error: look: {p} is empty", None, None
    if size > LOOK_MAX_BYTES:
        return (f"error: look: {p} is {size // 1024} KB, limit is "
                f"{LOOK_MAX_BYTES // (1024 * 1024)} MB", None, None)
    try:
        with p.open("rb") as f:
            data = f.read()
    except OSError as e:
        return f"error: look: cannot read {p}: {e.strerror or e}", None, None
    mime, short = _image_kind(data[:12])
    if mime is None:
        return (f"error: look: {p} is not JPEG, PNG or WebP "
                "(GIF/BMP are not supported)", None, None)
    text_line = f"image: {p} ({short}, {max(1, size // 1024)} KB)"
    return text_line, base64.b64encode(data).decode("ascii"), mime
