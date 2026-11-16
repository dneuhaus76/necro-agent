"""peer_status_loop - first check after 3 s, then 30 s loop; the Peer-API
line is green (EMBLEM) while a peer is online, neutral (DARK) otherwise."""
import json
import os
import tempfile
import threading

import pytest


class FakeApp:
    def __init__(self, blk=None):
        self.lock = threading.RLock()
        self.dirty = False
        self.peer_blk = blk


class FakeBlk:
    def __init__(self):
        self.color = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated peer discovery dir + import necro (no side effects beyond cfg read)."""
    peer_dir = tmp_path / "peers"
    peer_dir.mkdir()
    monkeypatch.setenv("NECRO_PEER_DIR", str(peer_dir))
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro, peer_dir


def _run_first_check(necro, app, blk, monkeypatch, instances):
    """Run peer_status_loop until the first check finished; returns the sleep calls."""
    from modules.io import api
    monkeypatch.setattr(api, "peer_instances", lambda: instances)
    monkeypatch.setattr(api, "OWN_ID", "self")
    sleeps = []

    class StopLoop(Exception):
        pass

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:  # startup delay + first loop delay -> stop here
            raise StopLoop
    monkeypatch.setattr(necro.time, "sleep", fake_sleep)

    def target():
        try:
            necro.peer_status_loop(app)
        except StopLoop:
            pass
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    return sleeps


def test_first_check_after_3s_then_30s(env, monkeypatch):
    necro, peer_dir = env
    blk = FakeBlk()
    app = FakeApp(blk)
    sleeps = _run_first_check(necro, app, blk, monkeypatch, [])
    assert sleeps[0] == 3   # first check after 3 s (was 30 s)
    assert sleeps[1] == 30  # then the regular 30 s loop


def test_online_peer_turns_line_green(env, monkeypatch):
    necro, peer_dir = env
    blk = FakeBlk()
    app = FakeApp(blk)
    inst = [{"id": "peer1", "pid": os.getpid(), "cwd": "/tmp", "port": 1, "token": "t"}]
    _run_first_check(necro, app, blk, monkeypatch, inst)
    assert blk.color == necro.EMBLEM
    assert app.dirty is True


def test_no_peer_stays_dark(env, monkeypatch):
    necro, peer_dir = env
    blk = FakeBlk()
    app = FakeApp(blk)
    _run_first_check(necro, app, blk, monkeypatch, [])
    assert blk.color == necro.DARK
    assert app.dirty is True


def test_own_instance_not_counted(env, monkeypatch):
    """OWN_ID is excluded - a running instance must not light up its own line."""
    necro, peer_dir = env
    blk = FakeBlk()
    app = FakeApp(blk)
    inst = [{"id": "self", "pid": os.getpid(), "cwd": "/tmp", "port": 1, "token": "t"}]
    _run_first_check(necro, app, blk, monkeypatch, inst)
    assert blk.color == necro.DARK


def test_new_session_replaces_block(env, monkeypatch):
    """/clear//new replaces the Peer-API block while the SAME loop runs - it must
    color the NEW block, not the discarded old one (regression: line stayed
    DARK after /new). The peer stays online, so no state change triggers it."""
    necro, peer_dir = env
    old_blk = FakeBlk()
    app = FakeApp(old_blk)
    inst = [{"id": "peer1", "pid": os.getpid(), "cwd": "/tmp", "port": 1, "token": "t"}]
    from modules.io import api
    monkeypatch.setattr(api, "peer_instances", lambda: inst)
    monkeypatch.setattr(api, "OWN_ID", "self")
    sleeps = []
    new_blk = FakeBlk()

    class StopLoop(Exception):
        pass

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:  # /new happens between check 1 and check 2
            app.peer_blk = new_blk
        if len(sleeps) >= 3:
            raise StopLoop
    monkeypatch.setattr(necro.time, "sleep", fake_sleep)

    def target():
        try:
            necro.peer_status_loop(app)
        except StopLoop:
            pass
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive()
    assert old_blk.color == necro.EMBLEM     # colored before /new
    assert new_blk.color == necro.EMBLEM     # recolored after /new (same state)
