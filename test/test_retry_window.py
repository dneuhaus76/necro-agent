"""fast start - the retry window must outlive a slow model load (old window: ~3 s)."""
import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NECRO_DB", str(tmp_path / "necro.db"))
    import necro
    return necro


class FastTime:
    """time.time() jumps past every wait window; sleep is a no-op (suite stays fast)."""
    def time(self):
        self.t = getattr(self, "t", 0.0) + 1000
        return self.t
    def sleep(self, s):
        pass


def _agent(env, monkeypatch):
    from modules.core import agent as agent_mod
    app = env.App()
    monkeypatch.setattr(agent_mod.time, "time", FastTime().time)
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    return app


def test_retry_survives_slow_model_load(env, monkeypatch):
    app = _agent(env, monkeypatch)
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 5:  # 4 failures: the old 3-attempt window raised here
            raise ConnectionRefusedError(111, "Connection refused")
        return "ok"
    assert app.agent._retry(flaky, "stream") == "ok"
    assert calls["n"] == 5


def test_retry_gives_up_after_max_attempts(env, monkeypatch):
    from modules.core import agent as agent_mod
    app = _agent(env, monkeypatch)
    calls = {"n": 0}
    def dead():
        calls["n"] += 1
        raise ConnectionRefusedError(111, "Connection refused")
    with pytest.raises(ConnectionRefusedError):
        app.agent._retry(dead, "stream")
    assert calls["n"] == agent_mod.RETRY_ATTEMPTS


def test_backoff_is_capped(env, monkeypatch):
    from modules.core import agent as agent_mod
    app = _agent(env, monkeypatch)
    def dead():
        raise ConnectionRefusedError(111, "Connection refused")
    with pytest.raises(ConnectionRefusedError):
        app.agent._retry(dead, "stream")
    waits = [b.text for b in app.blocks if "Retry in" in b.text]
    assert waits == [f"stream: ConnectionRefusedError - Retry in {w} s (Versuch {i + 1}/{agent_mod.RETRY_ATTEMPTS}) …"
                     for i, w in enumerate([1, 2, 4, 8, 10, 10, 10])]


def test_abort_stops_retrying(env, monkeypatch):
    app = _agent(env, monkeypatch)
    calls = {"n": 0}
    def dead():
        calls["n"] += 1
        app.stop.set()  # user pressed Ctrl+C during the first failure
        raise ConnectionRefusedError(111, "Connection refused")
    with pytest.raises(ConnectionRefusedError):
        app.agent._retry(dead, "stream")
    assert calls["n"] == 1
