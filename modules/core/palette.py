"""central color palette - single source of truth for all UI colors.
Roles -> 256-color index. Overridable per role via necro.cfg 'colors'
(role -> int 0-255). render.py (ui) derives the SGR strings from this;
config.py (core) validates the cfg values against ROLES.
Defaults are the user's table (see NECRO.md):
  necro_edit-old=208 #orange   necro_telegram=38 #blue
  necro_edit-new=28  #green    necro_agent=106   #green
  necro_user=216     #choc     necro_grey=250    #grey
  necro_dark=240     #dark     necro_red=167     #red
"""
PALETTE = {
    "edit-old": 216,   # orange    - edit diff "-" lines (background)
    "edit-new": 106,    # green     - edit diff "+" lines (background)
    "telegram": 38,    # blue      - telegram info line
    "agent": 106,      # green     - header / swarm info / prompt marker / emblem
    "user": 216,       # chocolate - user text + input line
    "grey": 250,       # grey      - agent markdown text
    "dark": 240,       # dark      - think / tool / info / separators
    "darker": 232,     # darker
    "red": 167,        # red       - error blocks
    "panel-bg": 236,   # dark panel - header / status line / code block background
    "header-fg": 255,  # white     - header + cwd text on the panel
    "code-fg": 252,    # light grey - code block text
    "edit-fg": 232,    # faded text on edit diff lines
}
EMOJI = {  # tool head symbols (cfg 'tool_emoji' overrides); default "›" for unknown tools
    "bash": "🦴", "edit_file": "✒️", "write_file": "✒️", "read_file": "📓",
    "consult": "🕷️", "delegate": "🕷️", "recall": "🧠", "peer": "💀",
    "thinking": "🧠",  # thinking-line prefix (not a tool head)
}
ROLES = frozenset(PALETTE)
def resolve(overrides):
    """
    @stable | verified: 2026-11 - frozen: change only on failing test or hard requirement
    Effective palette: defaults merged with cfg 'colors' (role -> 0-255).
    Unknown roles / bad values are ignored here (config.validate reports them)."""
    p = dict(PALETTE)
    if isinstance(overrides, dict):
        for role, idx in overrides.items():
            if role in PALETTE and isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx <= 255:
                p[role] = idx
    return p
