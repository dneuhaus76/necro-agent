"""PEERING is its own start parameter (env NECRO_PEER_API / cfg 'peer_api')
that activates the Peer-API/peer tool - like NECRO_SWARM for the swarm. It must
NOT activate the cfg swarm list (that stays with REMOTE / NECRO_SWARM)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_peering_flag_resolution(monkeypatch):
    """_flag: env wins over cfg, both win over the default (off)."""
    import necro
    monkeypatch.delenv("NECRO_PEER_API", raising=False)
    monkeypatch.setitem(necro.CFG, "peer_api", True)
    assert necro._flag("NECRO_PEER_API", "peer_api", False) is True
    monkeypatch.setenv("NECRO_PEER_API", "0")  # env off beats cfg on
    assert necro._flag("NECRO_PEER_API", "peer_api", False) is False
    monkeypatch.delenv("NECRO_PEER_API")
    monkeypatch.delitem(necro.CFG, "peer_api")
    assert necro._flag("NECRO_PEER_API", "peer_api", False) is False


def test_peering_activates_remote_not_swarm(monkeypatch):
    """PEERING on (REMOTE off): REMOTE becomes True (peer tool registered) but the
    cfg swarm list stays inactive (swarm_resolve gets the ORIGINAL remote value)."""
    import necro
    from modules.io.swarm import resolve as swarm_resolve
    monkeypatch.setenv("NECRO_PEER_API", "1")
    monkeypatch.delenv("NECRO_REMOTE", raising=False)
    monkeypatch.delenv("NECRO_SWARM", raising=False)
    monkeypatch.setitem(necro.CFG, "peer_api", True)
    monkeypatch.setitem(necro.CFG, "remote", False)
    monkeypatch.setitem(necro.CFG, "swarm", [{"model": "x"}])
    peering = necro._flag("NECRO_PEER_API", "peer_api", False)
    remote_orig = necro._flag("NECRO_REMOTE", "remote", False)
    remote = remote_orig or peering
    assert remote is True  # peer-API/peer tool active
    assert swarm_resolve(necro.CFG, None, remote_orig, "http://x/v1", "m") == []  # no cfg swarm
    # and with REMOTE on, the cfg swarm list IS active
    assert len(swarm_resolve(necro.CFG, None, True, "http://x/v1", "m")) == 1
