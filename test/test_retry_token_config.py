"""Code-review fixes (Session 66): one test per fixed bug.
1. Agent._retry swallowed a persistent 5xx: the HTTPError branch skipped the
   backoff and the loop fell out with a silent None (every caller then crashed
   far away from the cause). It must retry with backoff and finally raise.
2. Peer-API token was compared with '!=' (byte-wise) - with peer_host 0.0.0.0
   that leaks the token prefix over the LAN. Now secrets.compare_digest.
3. necro.cfg keys subagent_delegate / subagent_max_depth / max_output_tokens
   were read and documented but missing from config.KNOWN -> "unknown key".
"""
import threading
import time as _real_time

import pytest

from modules.core import inference
from modules.core.agent import Agent, RETRY_ATTEMPTS
from modules.core.config import validate
from modules.io.api import token_ok


class _App:
    """Minimal App stand-in: _retry only needs stop + the info sink."""
    def __init__(self):
        self.stop = threading.Event()
        self.notes = []

    def add(self, kind, text="", color=None):
        self.notes.append((kind, text))


def _virtual_clock(monkeypatch):
    """_retry waits in real 0.1 s slices, so a real backoff costs up to
    RETRY_MAX_WAIT seconds per attempt. Replace the module's clock with a
    virtual one: sleep advances time, so the loop still sees the full wait
    but the test finishes instantly."""
    class Clock:
        def __init__(self):
            self.t = 0.0
        def time(self):
            return self.t
        def sleep(self, s):
            self.t += s
        monotonic = staticmethod(_real_time.monotonic)  # "time" in this body is the method above
    monkeypatch.setattr("modules.core.agent.time", Clock())


def _agent(app):
    a = Agent.__new__(Agent)  # no construction: _retry touches app only
    a.app = app
    return a


def test_retry_5xx_raises_instead_of_returning_none(monkeypatch):
    """Bug 1 (regression): 8x HTTP 503 must raise the HTTPError, not return None."""
    _virtual_clock(monkeypatch)
    app = _App()
    calls = []

    def boom():
        calls.append(1)
        raise inference.HTTPError("server down", status=503)

    with pytest.raises(inference.HTTPError):
        _agent(app)._retry(boom, "complete")
    assert len(calls) == RETRY_ATTEMPTS  # retried, not given up on the first 5xx
    assert any("Retry in" in t for _, t in app.notes)  # backoff was announced


def test_retry_recovers_on_a_later_attempt(monkeypatch):
    """A transient 5xx followed by success returns the value."""
    _virtual_clock(monkeypatch)
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise inference.HTTPError("still loading", status=503)
        return "ok"

    assert _agent(_App())._retry(flaky, "complete") == "ok"
    assert state["n"] == 3


def test_retry_4xx_is_not_retried(monkeypatch):
    """4xx is a definitive rejection: one attempt, no backoff."""
    _virtual_clock(monkeypatch)
    calls = []

    def reject():
        calls.append(1)
        raise inference.HTTPError("bad request", status=400)

    with pytest.raises(inference.HTTPError):
        _agent(_App())._retry(reject, "complete")
    assert len(calls) == 1


def test_token_ok_accepts_only_the_exact_token():
    """Bug 2: constant-time compare, same accept/reject set as the old '!='."""
    assert token_ok({"X-Necro-Token": "abc123"}, "abc123")
    assert not token_ok({"X-Necro-Token": "abc124"}, "abc123")
    assert not token_ok({"X-Necro-Token": "abc"}, "abc123")
    assert not token_ok({}, "abc123")  # missing header = reject
    assert token_ok({}, "")  # both empty: token auth off


def test_config_knows_the_documented_opt_in_keys():
    """Bug 3: the three opt-in keys must not be reported as unknown."""
    assert validate({"subagent_delegate": True, "subagent_max_depth": 2,
                     "max_output_tokens": 512}) == []
    # wrong types are still caught (the point of KNOWN)
    assert validate({"subagent_delegate": "yes"})
    assert validate({"max_output_tokens": True})  # bool is not an int here
