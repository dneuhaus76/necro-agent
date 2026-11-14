"""Log block of the TUI: one user/agent/think/tool/info entry (state only, no rendering)."""

class Block:
    def __init__(self, kind, text="", color=None):
        self.kind, self.text, self.key, self.lines = kind, text, None, []
        self.color = color  # optional SGR base for info/error blocks (default: RED/DARK)
        self.live = False  # True once bash streamed its output into the block (agent._run_one reads it for every tool)
