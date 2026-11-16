"""Regression: Block must always expose a `live` attribute.

agent._run_one reads `blk.live` for EVERY tool call (not just bash). Before the
fix, Block.__init__ never set it, so any non-bash tool (grep/read/...) raised
AttributeError: 'Block' object has no attribute 'live' and killed the turn.
"""
from modules.core.block import Block


def test_block_has_live_default_false():
    b = Block("tool", "grep x")
    assert hasattr(b, "live")
    assert b.live is False


def test_live_can_be_set_true():
    b = Block("tool", "bash")
    b.live = True
    assert b.live is True
