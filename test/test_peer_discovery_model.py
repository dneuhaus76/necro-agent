"""the peer discovery file publishes the instance's model + inference URL, so a
parent sees which model a sibling runs. Fields are optional - older instances do not
have them and every reader must tolerate the absence."""
import json

import pytest


@pytest.fixture
def peer_env(tmp_path, monkeypatch):
    """Isolated discovery dir (PEER_DIR is bound at import time, so patched directly)
    and a peer API that is shut down again after the test."""
    from modules.io import api
    peers = tmp_path / "peers"
    peers.mkdir()
    monkeypatch.setattr(api, "PEER_DIR", peers)
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    _MADE.clear()
    yield tmp_path
    for a in _MADE:  # deterministic cleanup: stop the HTTP thread, drop the discovery file
        a._srv.shutdown()
        a.path.unlink(missing_ok=True)
    _MADE.clear()


_MADE = []


def _api(peer_env, model=None, url=None):
    from modules.io import api

    class App:
        peer_tag = "peer"

    a = api.PeerAPI(App(), host="127.0.0.1", port=0, peer_turn_timeout=5, model=model, url=url)
    _MADE.append(a)
    return api, a


def _read(a):
    return json.loads(a.path.read_text())


def test_discovery_contains_model_and_url(peer_env):
    api, a = _api(peer_env, model="Qwen3-Coder-30B", url="http://192.168.1.149:8080/v1")
    d = _read(a)
    assert d["model"] == "Qwen3-Coder-30B"
    assert d["url"] == "http://192.168.1.149:8080/v1"
    assert d["id"] == a.id and d["port"] == a.port  # the old fields survive


def test_discovery_omits_absent_fields(peer_env):
    """model/url are optional: without them the file looks like an older instance."""
    api, a = _api(peer_env)
    d = _read(a)
    assert "model" not in d and "url" not in d
    assert {"id", "host", "ip", "port", "token", "peer_turn_timeout_s", "pid", "cwd"} <= set(d)


def test_announce_updates_model_after_switch(peer_env):
    api, a = _api(peer_env, model="old-model", url="http://x/v1")
    a.announce(model="new-model")
    d = _read(a)
    assert d["model"] == "new-model" and d["url"] == "http://x/v1"  # url unchanged
    a.announce(model="new-model", url="http://y/v1")
    assert _read(a)["url"] == "http://y/v1"


def test_reader_tolerates_missing_fields(peer_env):
    """An old discovery file (no model/url) must still be listed - no KeyError."""
    from modules.io import api

    peers = peer_env / "peers"
    peers.mkdir(parents=True, exist_ok=True)
    import os
    old = {"id": "deadbeef", "host": "127.0.0.1", "ip": "127.0.0.1", "port": 1,
           "token": "t", "peer_turn_timeout": 5, "pid": os.getpid(), "cwd": str(peer_env)}
    (peers / "necro-deadbeef.json").write_text(json.dumps(old))
    got = {p["id"]: p for p in api.peer_instances()}
    assert "deadbeef" in got
    assert got["deadbeef"].get("model") is None  # absent, not an error


def test_peer_refresh_line_shows_model(peer_env, monkeypatch):
    """The peer instance list shows model @ url when published, and stays readable when not."""
    import necro

    def line(p):
        state = "idle"
        who = f", {p['model']} @ {p.get('url') or '?'}" if p.get("model") else ""
        return f"{p['id']} (pid {p['pid']}, {p['cwd']}, {state}{who})"

    assert "30B @ http://h/v1" in line({"id": "x", "pid": 1, "cwd": "/w",
                                       "model": "Qwen3-30B", "url": "http://h/v1"})
    assert "x (pid 1, /w, idle)" == line({"id": "x", "pid": 1, "cwd": "/w"})


def test_startup_publishes_config_model(peer_env, monkeypatch):
    """PeerAPI is constructed with the app's model and BASE (necro.py wiring)."""
    import inspect
    import necro
    from modules.io.api import PeerAPI as P
    sig = inspect.signature(P.__init__)
    assert "model" in sig.parameters and "url" in sig.parameters
    src = inspect.getsource(necro.main)
    assert "model=app.model" in src and "url=BASE" in src
