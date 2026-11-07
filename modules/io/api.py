"""Peer API: a tiny local HTTP server so sibling NECRO instances can delegate
full agent turns to each other. Ephemeral port (bind 0), discovery via /tmp/necro/necro-{id}.json,
token auth from the discovery file. One-shot: the prompt runs as a normal turn in the peer's UI."""
import json, os, queue, socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
# own subdir so the host only needs to mount /tmp/necro (not all of /tmp)
PEER_DIR = Path(os.environ.get("NECRO_PEER_DIR", "/tmp/necro"))
PEER_DIR.mkdir(parents=True, exist_ok=True)
PEER_TURN_TIMEOUT = 600  # max wall time of a peer turn - watchdog aborts it
OWN_ID = None  # set by PeerAPI.__init__ - unique per instance, PID is not (containers share PIDs)
_LOCAL_IP = None
def local_ip():
    """Best-effort container/host IP (for cross-container peers), cached -
    peer_instances() runs every step in remote mode, the UDP probe stays once."""
    global _LOCAL_IP
    if _LOCAL_IP is None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            _LOCAL_IP = s.getsockname()[0]
            s.close()
        except OSError:
            return "127.0.0.1"  # not cached: a transient failure must not stick
    return _LOCAL_IP
def _http_alive(ip, port, token, timeout=2):
    """Cross-container liveness: PID namespace differs, so use the HTTP health endpoint."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://{ip}:{port}/api/health",
                                     headers={"X-Necro-Token": token})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
def peer_instances():
    """Running sibling NECRO instances from /tmp/necro-*.json (stale files of dead
    pids are skipped). Each entry: {id, port, token, pid, cwd}."""
    out = []
    my_ip = local_ip()
    for f in sorted(PEER_DIR.glob("necro-*.json")):
        try:
            d = json.loads(f.read_text())
        except (ValueError, OSError):
            # transient (the file is being written right now) or a malformed
            # entry - keep it, the next check sees the finished write; unlinking
            # here would drop a starting instance's discovery file
            continue
        peer_ip = d.get("ip", "127.0.0.1")
        if peer_ip not in ("127.0.0.1", my_ip):
            # cross-container peer - PID namespace differs, use HTTP health check
            if not _http_alive(peer_ip, d["port"], d.get("token", "")):
                try:
                    f.unlink()  # stale: peer unreachable
                except OSError:
                    pass
                continue
        else:
            # local peer: PID liveness check
            try:
                os.kill(d["pid"], 0)
            except ProcessLookupError:
                try:
                    f.unlink()  # stale: process is gone
                except OSError:
                    pass
                continue
            except PermissionError:
                pass  # the pid lives but belongs to another user - keep it
            except Exception:
                continue  # malformed entry (no pid / wrong type): keep, re-check next time
        out.append(d)
    return out
def peer_health(inst, timeout=2):
    """Pre-flight check before a (potentially long) peer task - the peer must be
    reachable. (busy is still reported: the peer queues instead of rejecting.)"""
    import urllib.request
    host = inst.get("ip") or "127.0.0.1"
    try:
        req = urllib.request.Request(f"http://{host}:{inst['port']}/api/health",
                                     headers={"X-Necro-Token": inst.get("token", "")})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.loads(r.read()).get("busy")), None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
def peer_prompt(inst, task, timeout=PEER_TURN_TIMEOUT):
    """one-shot peer prompt call (HTTP POST /api/prompt). Returns the result
    string or an error string - the caller (run_tool) just forwards it."""
    import urllib.request
    host = inst.get("ip") or "127.0.0.1"  # cross-container peers use their real IP
    req = urllib.request.Request(f"http://{host}:{inst['port']}/api/prompt",
                                 json.dumps({"task": task, "from": OWN_ID}).encode(),
                                 {"Content-Type": "application/json", "X-Necro-Token": inst["token"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            o = json.loads(r.read())
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"
    return f"[peer {inst['id']}] {o['result']} ({o['tool_calls']} tool calls)"
class PeerAPI:
    def __init__(self, app, host="127.0.0.1", port=0):
        """host/port configurable: Docker needs 0.0.0.0 + a fixed, exposed port."""
        global OWN_ID
        self.app = app
        self.id = os.urandom(4).hex()
        OWN_ID = self.id  # self-identity for sibling filtering (PID is not unique across containers)
        self.token = os.urandom(12).hex()
        self.host = host
        self.bind_error = None
        try:
            srv = ThreadingHTTPServer((host, port), _make_handler(self))
        except OSError as e:
            # EADDRNOTAVAIL: IP is not a local interface -> bind 0.0.0.0;
            # EADDRINUSE: port taken -> keep host, ephemeral port.
            # Either way a bad peer config must not kill startup.
            self.bind_error = f"{host}:{port} -> {e.strerror}"
            fb_host = "0.0.0.0" if e.errno == getattr(socket, "EADDRNOTAVAIL", 99) else host
            self.host = fb_host
            srv = ThreadingHTTPServer((fb_host, 0), _make_handler(self))
        self.port = srv.server_address[1]
        # incoming peer turns are queued and run one after another -
        # a second POST no longer gets 409 (the sender would have to retry itself)
        app._peer_queue = queue.Queue()
        threading.Thread(target=self._peer_worker, daemon=True).start()
        self.path = PEER_DIR / f"necro-{self.id}.json"
        self.path.write_text(json.dumps({"id": self.id, "host": self.host,
                                         "ip": local_ip() if self.host in ("0.0.0.0", "::") else "127.0.0.1",
                                         "port": self.port, "token": self.token,
                                         "pid": os.getpid(), "cwd": os.getcwd()}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    def _peer_worker(self):
        """Drain the incoming peer-turn queue - one turn at a time, in order."""
        while True:
            msg, frm, ev = self.app._peer_queue.get()
            task = msg["content"]
            # a LOCAL turn may have started after the previous peer turn ended -
            # wait for it (and for the queue to drain) before starting this one,
            # otherwise two turns would run at once and race on app.messages.
            # The user message is already in app.messages (appended by the POST
            # handler under the lock); qsize() is not atomic with get(), but the
            # 0.2 s re-check absorbs the race: a concurrently enqueued item is
            # seen on the next loop pass.
            while self.app.busy or self.app._peer_queue.qsize():
                time.sleep(0.2)
            self.app.add("user", f"{self.app.peer_tag} [{frm}] {task}")
            self.app.db_sync()
            self.app.stop.clear()
            self.app._peer_turn = True  # peer turn - use SUB_TOOLS (no peer tool, avoid recursion)
            self.app.busy = True  # visible to health + queues local turns; turn()'s finally releases it
            # a hung turn must not hold busy forever - the watchdog aborts it
            # (stop is checked in the stream loop and between steps), which releases busy
            watchdog = threading.Timer(PEER_TURN_TIMEOUT, self.app.stop.set)
            watchdog.start()
            try:
                self.app.turn(done=ev)
            finally:
                watchdog.cancel()
                self.app._peer_turn = False  # reset for the next local/peer turn
    def close(self):
        try:
            self.path.unlink()
        except OSError:
            pass
def _make_handler(api):
    class H(BaseHTTPRequestHandler):
        timeout = PEER_TURN_TIMEOUT + 30  # a full agent turn can take minutes; +30 s so the
                                          # response still goes out after ev.wait() times out
        def log_message(self, *a):
            pass
        def _ok(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        def do_GET(self):
            if self.path == "/api/health":
                self._ok(200, {"id": api.id, "busy": api.app.busy})
            else:
                self._ok(404, {"error": "not found"})
        def do_POST(self):
            if self.headers.get("X-Necro-Token") != api.token:
                return self._ok(401, {"error": "bad token"})
            if self.path != "/api/prompt":
                return self._ok(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))  # bad header -> 400, not 500
                d = json.loads(self.rfile.read(n))
                task = d.get("task", "")
                frm = d.get("from") or "?"  # sender's id (own id would be a lie)
            except ValueError:
                return self._ok(400, {"error": "invalid JSON"})
            app = api.app
            with app.lock:
                # identity, not index (see below); the list keeps the old dicts
                # alive so their ids cannot be recycled by GC into new messages
                before_msgs = list(app.messages)
                before = {id(m) for m in before_msgs}
                # tool-call ids of the OLD context - trim_tool_results() (compact)
                # swaps dicts of oversized tool messages mid-turn, so id(m) is not a
                # stable identity for them; tool_call_id is (it comes from the model)
                before_tci = {m.get("tool_call_id") for m in before_msgs if m.get("role") == "tool"}
                msg = {"role": "user", "content": task}
                app.messages.append(msg)
                app.scroll = 0
            # enqueue instead of 409 - the worker thread runs the turn after
            # the current one (local or peer) finishes; the POST blocks until then.
            # The message dict is passed by reference: a queued turn's user message
            # must already be in the context when ITS turn runs (not the next one's).
            ev = threading.Event()
            app._peer_queue.put((msg, frm, ev))
            ev.wait(timeout=PEER_TURN_TIMEOUT)
            # only messages of THIS turn - a turn that ended without a new assistant
            # reply (error/abort/timeout) must return '(no output)', not a stale reply
            # from an earlier conversation. matched by IDENTITY, not by index -
            # compact() may replace/shrink app.messages mid-turn, which would make the
            # old messages[before:] slice empty (reply lost -> '(no output)') or wrong.
            _ = before_msgs  # keep alive until both lookups below are done
            text = next((m["content"] for m in reversed(app.messages)
                         if m["role"] == "assistant" and m.get("content") and id(m) not in before), "")
            ntools = sum(1 for m in app.messages if m["role"] == "tool" and m.get("tool_call_id") not in before_tci)
            self._ok(200, {"result": text or "(no output)", "tool_calls": ntools})
    return H
