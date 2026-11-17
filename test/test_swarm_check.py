"""the swarm availability check retries - a single transient network hiccup
must not flip an agent to 'off' for the whole 30 s cycle. check_once probes each
agent up to CHECK_ATTEMPTS times before marking it offline."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import modules.io.swarm as swarm
from modules.io.swarm import check_once, CHECK_ATTEMPTS


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(swarm, "CHECK_RETRY_DELAY", 0.01)  # keep the suite < 5 s


class FlakyHandler(BaseHTTPRequestHandler):
    """fails the first N requests (connection closed without a response), then serves
    /models with 200 and every other path with 404 (a real runner answers only one
    of the probe endpoints - the probe must not count a 404 as 'up')."""
    fails_left = 2

    def do_GET(self):
        if type(self).fails_left > 0:
            type(self).fails_left -= 1
            self.close_connection = True
            return  # no response written -> client sees RemoteDisconnected
        self.send_response(200 if self.path == "/models" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def start_server(fails_left):
    FlakyHandler.fails_left = fails_left
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_transient_failure_retried():
    srv, url = start_server(fails_left=CHECK_ATTEMPTS - 1)  # last attempt succeeds
    try:
        up = check_once([{"url": url, "model": "m"}])
        assert up[(url, "m")] is True
    finally:
        srv.shutdown()


def test_permanent_failure_offline():
    # every attempt fails: the probe tries all endpoints per attempt, so the
    # flaky counter must cover attempts * endpoints
    srv, url = start_server(fails_left=CHECK_ATTEMPTS * len(swarm.PROBE_PATHS))
    try:
        up = check_once([{"url": url, "model": "m"}])
        assert up[(url, "m")] is False
    finally:
        srv.shutdown()


def test_check_pass_fits_in_interval():
    """F2: one full availability pass (worst case, all agents fail every attempt)
    must stay under the 30 s check interval, else the effective status latency
    doubles (pass 31.5 s + sleep 30 s = 61.5 s instead of ~30 s)."""
    from modules.io.swarm import CHECK_ATTEMPTS, CHECK_RETRY_DELAY, PROBE_PATHS
    agents = 3  # current cfg swarm size
    probe_timeout = 2  # s per endpoint (see _probe)
    worst_case = agents * CHECK_ATTEMPTS * (len(PROBE_PATHS) * probe_timeout + CHECK_RETRY_DELAY)
    assert worst_case < 30, f"full check pass {worst_case}s exceeds the 30s interval"


def test_dead_port_offline():
    # port 1 on 127.0.0.1 is almost certainly closed -> connection refused
    up = check_once([{"url": "http://127.0.0.1:1", "model": "m"}])
    assert up[("http://127.0.0.1:1", "m")] is False
