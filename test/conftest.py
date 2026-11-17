"""Test isolation: no test may leave a running background service behind.

Two leaks made the suite slower with every run and finally blocked the whole
container: (1) every PeerAPI object started an accept loop plus a worker thread
and nothing stopped them - ~39 objects piled up ~78 idle threads; (2) every
TelegramBot started a long-poll loop that talked to the REAL api.telegram.org
(invalid token, no route -> SSL retry every 3 s). Measured CPU-bound work at the
end of a full run was 9x slower than in a clean process, so the suite "hung"
even after the last test.
"""
import pytest


@pytest.fixture(autouse=True)
def close_peer_apis():
    """Close every PeerAPI a test built and did not close itself."""
    yield
    from modules.io import api
    api.close_all()


@pytest.fixture(autouse=True)
def no_telegram_poll(monkeypatch):
    """TelegramBot starts a long-poll thread in __init__. A test must not reach
    the real Bot API - it would fight with the live NECRO bot for the updates
    (Telegram answers 409 to the second poller). Only the poll loop is disabled;
    _route() and send() stay testable."""
    from modules.io import tg
    monkeypatch.setattr(tg.TelegramBot, "_loop", lambda self: None)
