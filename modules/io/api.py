"""Peer API: a tiny local HTTP server so sibling NECRO instances can delegate
full agent turns to each other. Ephemeral port (bind 0), discovery via /tmp/necro/necro-{id}.json,
token auth from the discovery file. One-shot: the prompt runs as a normal turn in the peer's UI."""
import json, os, queue, socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
# own subdir so the host only needs to mount /tmp/necro (not all of /tmp)
PEER_DIR = Path(os.environ.get("NECRO_PEER_DIR", "/tmp/necro"))
PEER_DIR.mkdir(parents=True, exist_ok=True)
PEER_TURN_TIMEOUT = 600  # fallback (s) - max wall time of a peer turn; cfg 'peer_turn_timeout' / env wins
OWN_ID = None  # set by PeerAPI.__init__ - unique per instance, PID is not (containers share PIDs)
def _atomic_write(path, text):
    """tmp + os.replace in the same dir - a crash mid-write never leaves a half file
    that peer_instances()/peer_prompt() would choke on."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
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
_PEER_FAILURE_SINCE = {}  # {path_str: timestamp} - first-seen failure for grace period
PEER_GRACE_PERIOD = 30  # seconds of consecutive failures before hard-unlinking a cross-container peer
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
            # cross-container peer - PID namespace differs, use HTTP health check.
            # A single failure is transient (network blip, slow peer) - soft-exclude
            # from the return list. Hard-unlink only after PEER_GRACE_PERIOD of
            # consecutive failures (the peer is truly gone).
            if not _http_alive(peer_ip, d["port"], d.get("token", "")):
                key = str(f)
                now = time.time()
                since = _PEER_FAILURE_SINCE.get(key, now)
                _PEER_FAILURE_SINCE[key] = since
                if now - since > PEER_GRACE_PERIOD:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                    _PEER_FAILURE_SINCE.pop(key, None)
                continue
            else:
                _PEER_FAILURE_SINCE.pop(str(f), None)  # alive again, reset timer
        else:
            # local peer: PID liveness check (same PID namespace - confirmed death)
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
_PEER_BUSY = {}  # {id: (busy, ts)} - cached busy status for listing/routing
PEER_BUSY_TTL = 5  # seconds - the status is a hint, not a contract
def peer_busy(inst, timeout=1):
    """Cached busy status of a peer (TTL PEER_BUSY_TTL) for the tool description
    and idle-first routing. None when unknown (never checked / check failed)."""
    now = time.time()
    c = _PEER_BUSY.get(inst["id"])
    if c is not None and now - c[1] < PEER_BUSY_TTL:
        return c[0]
    busy, err = peer_health(inst, timeout=timeout)
    if err is None:
        _PEER_BUSY[inst["id"]] = (busy, now)
        return busy
    return c[0] if c is not None else None  # stale beats unknown
# parent-child tree: claims keep the source from addressing a grandchild
# directly. When my child X calls Y, X notifies me (its parent) via /api/claim
# and I exclude Y from my peer list while Y's turn runs. Module-global (like
# OWN_ID): per-process state of THIS instance.
_PEER_CLAIMS = {}  # {child_id: (claimer_id, ts)}
_PEER_CLAIMS_LOCK = threading.Lock()
PEER_CLAIM_TTL = 900  # s - backstop for a crashed claimer (the chain budget is shorter)
def peer_set_claim(child_id, claimer_id, op="claim"):
    """Record (claim) or drop (release) a grandchild claim on THIS instance."""
    with _PEER_CLAIMS_LOCK:
        if op == "claim":
            _PEER_CLAIMS[child_id] = (claimer_id, time.time())
        else:
            _PEER_CLAIMS.pop(child_id, None)
def peer_excluded_ids():
    """Ids to keep out of the peer list: claimed by my children (grandchildren),
    not stale (the TTL covers a crashed claimer; dead peers drop out of the
    list via discovery anyway). Stale entries are removed here (the function is
    called on every peer call, so this is the natural cleanup point - without
    it a crashed claimer would leak its entry forever, unbounded growth)."""
    now = time.time()
    with _PEER_CLAIMS_LOCK:
        stale = [c for c, (_, ts) in _PEER_CLAIMS.items() if now - ts >= PEER_CLAIM_TTL]
        for c in stale:
            del _PEER_CLAIMS[c]
        return set(_PEER_CLAIMS)
def peer_claim(inst, child_id, op="claim", timeout=2):
    """Notify inst (my parent) that child_id is my child ('claim') or free again
    ('release'). Best-effort: a failed notification only weakens the topology
    hint, it never breaks the chain."""
    import urllib.request
    host = inst.get("ip") or "127.0.0.1"
    try:
        req = urllib.request.Request(f"http://{host}:{inst['port']}/api/claim",
                                     json.dumps({"child": child_id, "op": op, "from": OWN_ID}).encode(),
                                     {"Content-Type": "application/json", "X-Necro-Token": inst.get("token", "")})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
def peer_prompt(inst, task, timeout=None, hops=1, budget_left=None):
    """one-shot peer prompt call (HTTP POST /api/prompt). Returns the result
    string or an error string - the caller (run_tool) just forwards it.
    hops: hop number of the receiving turn in the chain (1 = direct call from a
    local turn); budget_left: remaining wall-clock seconds of the whole chain
    (the receiver's watchdog is capped by it). timeout: wall time of the peer
    turn + queue wait (default: the peer's own configured peer_turn_timeout,
    read from its discovery file, capped by budget_left)."""
    import urllib.request
    host = inst.get("ip") or "127.0.0.1"  # cross-container peers use their real IP
    if timeout is None:
        # the PEER decides how long its turn may run - its discovery file carries
        # its own configured value (written at startup); +30 s so a response that
        # goes out right after the peer's ev.wait() timeout still arrives in time
        try:
            timeout = int(inst.get("peer_turn_timeout", PEER_TURN_TIMEOUT)) + 30
        except (ValueError, TypeError):
            timeout = PEER_TURN_TIMEOUT + 30  # malformed value in the discovery file
        if budget_left is not None:
            timeout = min(timeout, budget_left + 30)  # the chain budget wins
    body = {"task": task, "from": OWN_ID, "hops": hops}
    if budget_left is not None:
        body["budget_left"] = budget_left
    req = urllib.request.Request(f"http://{host}:{inst['port']}/api/prompt",
                                 json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Necro-Token": inst["token"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            o = json.loads(r.read())
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"
    return f"[peer {inst['id']}] {o['result']} ({o['tool_calls']} tool calls)"
class PeerAPI:
    def __init__(self, app, host="127.0.0.1", port=0, turn_timeout=PEER_TURN_TIMEOUT):
        """host/port configurable: Docker needs 0.0.0.0 + a fixed, exposed port.
        turn_timeout (s): max wall time of a peer turn (watchdog + HTTP waits)."""
        global OWN_ID
        self.app = app
        self.turn_timeout = turn_timeout
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
        _atomic_write(self.path, json.dumps({"id": self.id, "host": self.host,
                                             "ip": local_ip() if self.host in ("0.0.0.0", "::") else "127.0.0.1",
                                             "port": self.port, "token": self.token,
                                             "peer_turn_timeout": self.turn_timeout,
                                             "pid": os.getpid(), "cwd": os.getcwd()}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    def _peer_worker(self):
        """Drain the incoming peer-turn queue - one turn at a time, in order."""
        while True:
            msg, frm, ev, hops, budget_left = self.app._peer_queue.get()
            task = msg["content"]
            # a LOCAL turn may have started after the previous peer turn ended -
            # wait for it before starting this one, otherwise two turns would
            # run at once and race on app.messages. The worker is the sole
            # consumer of the queue, so items are naturally processed one at a
            # time; no qsize() check needed (that would deadlock with 2+ items).
            while self.app.busy:
                time.sleep(0.2)
            self.app.add("user", f"{self.app.peer_tag}: {task}")
            self.app.db_sync()
            self.app.stop.clear()
            # peer turn - toolset is built by App.turn from hops (peer tool only
            # while hops < limit) and the subagent toggle
            self.app._peer_turn = True
            self.app._peer_hops = hops  # hop number of THIS turn in the chain (1 = direct)
            self.app._peer_from = frm  # sender id - bounce filter (no call back to the caller)
            self.app.busy = True  # visible to health + queues local turns; turn()'s finally releases it
            # a hung turn must not hold busy forever - the watchdog aborts it
            # (stop is checked in the stream loop and between steps), which releases busy
            # a chain budget caps the watchdog - the whole chain shares one
            # wall-clock budget, not one full peer_turn_timeout per hop. The budget
            # and its start time are stored on the app so a peer call made DURING
            # this turn can pass the remaining time to the next hop.
            tmo = self.turn_timeout
            if budget_left is not None:
                try:
                    # floor 0.2 s (not 1 s) so sub-second chain budgets still cap
                    # the watchdog; float (not int) keeps fractional budgets precise
                    tmo = min(tmo, max(0.2, float(budget_left)))
                except (ValueError, TypeError):
                    pass  # malformed budget in the body - fall back to the own timeout
            self.app._chain_t0 = time.monotonic()
            self.app._chain_budget = tmo
            # the watchdog marks itself so turn()'s except-block can tell a
            # timeout apart from a user abort (both set stop) - the parent then
            # gets "turn timeout (watchdog, N s)" instead of a bare "aborted"
            self.app._watchdog_fired = None
            def _wd_fire():
                self.app._watchdog_fired = tmo
                self.app.stop.set()
            watchdog = threading.Timer(tmo, _wd_fire)
            watchdog.start()
            try:
                self.app.turn(done=ev)
            finally:
                watchdog.cancel()
                self.app._peer_turn = False  # reset for the next local/peer turn
                self.app._peer_hops = 0
                self.app._peer_from = None
    def close(self):
        try:
            self.path.unlink()
        except OSError:
            pass
def _make_handler(api):
    class H(BaseHTTPRequestHandler):
        timeout = api.turn_timeout + 30  # a full agent turn can take minutes; +30 s so the
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
            if self.path == "/api/claim":  # parent-child topology hint (best-effort)
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    d = json.loads(self.rfile.read(n))
                    child = str(d.get("child", ""))
                    if child:
                        peer_set_claim(child, d.get("from") or "?", d.get("op", "claim"))
                except ValueError:
                    return self._ok(400, {"error": "invalid JSON"})
                return self._ok(200, {"ok": True})
            if self.path != "/api/prompt":
                return self._ok(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))  # bad header -> 400, not 500
                d = json.loads(self.rfile.read(n))
                task = d.get("task", "")
                frm = d.get("from") or "?"  # sender's id (own id would be a lie)
                hops = d.get("hops", 1)  # hop number of the receiving turn
                budget_left = d.get("budget_left")  # remaining chain wall-clock
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
            app._peer_queue.put((msg, frm, ev, hops, budget_left))
            ev.wait(timeout=api.turn_timeout)
            # only messages of THIS turn - a turn that ended without a new assistant
            # reply (error/abort/timeout) must return '(no output)', not a stale reply
            # from an earlier conversation. matched by IDENTITY, not by index -
            # compact() may replace/shrink app.messages mid-turn, which would make the
            # old messages[before:] slice empty (reply lost -> '(no output)') or wrong.
            _ = before_msgs  # keep alive until both lookups below are done
            # identity of the assistant message appended by THIS turn (set in the
            # turn loop); decoupled from the tail-invariant (compact() keep-window)
            # and from CPython id()/GC semantics of a reverse-scan.
            last = getattr(app, "_last_reply", None)
            text = last.get("content", "") if last and id(last) not in before else ""
            ntools = sum(1 for m in app.messages if m["role"] == "tool" and m.get("tool_call_id") not in before_tci)
            if text:
                result = text
            else:
                # no reply this turn. Distinguish a clean reasoning-only end
                # from an abort so the parent can act (timeout -> raise budget /
                # shorten task; stagnation -> reword; error -> retry). _last_turn_error
                # is reset at the start of every turn, so it reflects THIS turn.
                terr = getattr(app, "_last_turn_error", None)
                result = f"(aborted: {terr})" if terr else "(no output)"
            self._ok(200, {"result": result, "tool_calls": ntools})
    return H
