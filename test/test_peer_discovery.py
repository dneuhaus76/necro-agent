"""cross-container peer with transient health failure is soft-excluded
(not hard-unlinked) until the grace period expires."""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def peer_dir(tmp_path, monkeypatch):
    d = tmp_path / "peers"
    d.mkdir()
    monkeypatch.setenv("NECRO_PEER_DIR", str(d))
    # re-import to pick up the new PEER_DIR
    import importlib
    import modules.io.api as api_mod
    importlib.reload(api_mod)
    return api_mod, d


def _write_peer(d, peer_id="abc123", ip="192.168.1.99", port=9999, token="tok"):
    f = d / f"necro-{peer_id}.json"
    f.write_text(json.dumps({"id": peer_id, "host": "0.0.0.0", "ip": ip,
                             "port": port, "token": token, "pid": 99999,
                             "cwd": "/tmp"}))
    return f


def test_transient_failure_soft_exclude(peer_dir):
    """A single health failure excludes the peer from the list but does NOT
    unlink the discovery file (grace period not yet expired)."""
    api_mod, d = peer_dir
    f = _write_peer(d)
    api_mod._PEER_FAILURE_SINCE.clear()

    with patch.object(api_mod, "_http_alive", return_value=False):
        result = api_mod.peer_instances()
        assert result == []  # excluded from return list
        assert f.exists()  # file still there (soft exclude, not hard unlink)


def test_grace_period_expiry_hard_unlink(peer_dir):
    """After PEER_GRACE_PERIOD of consecutive failures, the file is hard-unlinked."""
    api_mod, d = peer_dir
    f = _write_peer(d)
    api_mod._PEER_FAILURE_SINCE.clear()

    with patch.object(api_mod, "_http_alive", return_value=False):
        # first call: records failure time
        api_mod.peer_instances()
        assert f.exists()
        # simulate time passing beyond the grace period
        api_mod._PEER_FAILURE_SINCE[str(f)] = time.time() - api_mod.PEER_GRACE_PERIOD - 1
        result = api_mod.peer_instances()
        assert result == []
        assert not f.exists()  # hard-unlinked after grace period


def test_recovery_resets_timer(peer_dir):
    """If the peer comes back alive, the failure timer resets."""
    api_mod, d = peer_dir
    f = _write_peer(d)
    api_mod._PEER_FAILURE_SINCE.clear()

    with patch.object(api_mod, "_http_alive", return_value=False):
        api_mod.peer_instances()
        assert str(f) in api_mod._PEER_FAILURE_SINCE

    with patch.object(api_mod, "_http_alive", return_value=True):
        result = api_mod.peer_instances()
        assert len(result) == 1  # peer is back
        assert str(f) not in api_mod._PEER_FAILURE_SINCE  # timer reset


def test_local_pid_death_still_hard_unlinks(peer_dir):
    """Local peer (127.0.0.1) with dead PID is still hard-unlinked immediately
    (same PID namespace = confirmed death, no grace period needed)."""
    api_mod, d = peer_dir
    # use a PID that is almost certainly dead
    f = _write_peer(d, ip="127.0.0.1", port=9999)
    # rewrite with a dead pid
    data = json.loads(f.read_text())
    data["pid"] = 999999  # unlikely to exist
    f.write_text(json.dumps(data))

    result = api_mod.peer_instances()
    assert result == []
    assert not f.exists()
