"""PeerAPI.close() must really stop the service.

It used to unlink the discovery file only: the accept loop and the worker thread
kept running, so every test that built a PeerAPI left two threads behind. 39
such objects were enough to slow CPU-bound work in the pytest process by 9x -
the suite looked hung for minutes after the last test.
"""
import threading
import time

import pytest


@pytest.fixture
def api_mod(tmp_path, monkeypatch):
    from modules.io import api
    monkeypatch.setattr(api, "PEER_DIR", tmp_path)
    monkeypatch.setattr(api, "PEER_CLAIM_TTL", 60)
    return api


def _app():
    app = type("A", (), {})()
    app.busy, app.stop, app.dirty = False, threading.Event(), True
    return app


def test_close_stops_both_threads(api_mod):
    before = threading.active_count()
    p = api_mod.PeerAPI(_app())
    path = p.path
    assert threading.active_count() == before + 2, "accept loop + worker started"
    assert path.exists(), "discovery file published"
    p.close()
    deadline = time.monotonic() + 3
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert threading.active_count() == before, f"threads left: {threading.enumerate()}"
    assert not path.exists(), "discovery file removed"


def test_close_is_idempotent(api_mod):
    p = api_mod.PeerAPI(_app())
    p.close()
    p.close()  # a second close in a shutdown path must not raise


def test_close_all_clears_the_registry(api_mod):
    made = [api_mod.PeerAPI(_app()) for _ in range(3)]
    api_mod.close_all()
    assert not list(api_mod._LIVE)
    assert not any(m.path.exists() for m in made)
