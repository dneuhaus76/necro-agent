"""Central color palette - single source of truth for all UI colors.

Every role colors exactly one part of the screen (see ROLE_DOC). The palette is
a 256-color table: a role maps to an index 0-255. necro.cfg 'colors' overrides
single roles; render.py (ui) derives the SGR strings from the effective palette;
config.py (core) validates the cfg values against these roles.

Help: `./necro.py --colors` (or `python3 -m modules.core.palette`) prints the
table below with role, index and purpose.
"""
PALETTE = {
    "user": 216,       # user text, input line, prompt marker
    "text": 244,       # agent answer text (markdown body)
    "accent": 106,     # header title, emblem, important tool heads, online swarm
    "muted": 240,      # think/tool detail lines, separators, secondary text
    "error": 167,      # error blocks and error text
    "telegram": 38,    # telegram info line
    "panel_bg": 236,   # background of header, status line and code blocks
    "panel_text": 255, # header and cwd text on the panel
    "code_text": 252,  # text inside code blocks
    "edit_text": 232,  # text on edit diff lines
    "edit_del_bg": 216,# background of removed lines in an edit diff
    "edit_add_bg": 106,# background of added lines in an edit diff
}
# what each role colors - the help text for the cfg, one line per role.
# A new role without a line here fails the palette test.
ROLE_DOC = {
    "user": "user text, input line, prompt marker",
    "text": "agent answer text (markdown body)",
    "accent": "header title, emblem, important tool heads, online swarm",
    "muted": "think/tool detail lines, separators, secondary text",
    "error": "error blocks and error text",
    "telegram": "telegram info line",
    "panel_bg": "background of header, status line and code blocks",
    "panel_text": "header and cwd text on the panel",
    "code_text": "text inside code blocks",
    "edit_text": "text on edit diff lines",
    "edit_del_bg": "background of removed lines in an edit diff",
    "edit_add_bg": "background of added lines in an edit diff",
}
EMOJI = {  # tool head symbols (cfg 'tool_emoji' overrides); "▸" for unknown tools
    "bash": "🦴", "edit_file": "✒️", "write_file": "✒️", "read_file": "📓",
    "consult": "🕷️", "delegate": "🕷️", "recall": "🧠", "peer": "💀", "grep": "🔍",
    "thinking": "🧠",  # thinking-line prefix (not a tool head)
}
ROLES = frozenset(PALETTE)
def is_index(value):
    """True for a usable 256-color index (0-255, int or numeric string, not bool)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            return False
        value = int(value)
    return isinstance(value, int) and 0 <= value <= 255
def resolve(overrides):
    """
    @stable | verified: 2026-11 - frozen: change only on failing test or hard requirement
    Effective palette: defaults merged with cfg 'colors'. Unknown roles and bad
    values are ignored here (config.validate reports them)."""
    p = dict(PALETTE)
    if isinstance(overrides, dict):
        for role, value in overrides.items():
            if role in PALETTE and is_index(value):
                p[role] = int(value)
    return p
def table():
    """Role, index, purpose - the exact help a user needs to edit cfg 'colors'."""
    width = max(len(r) for r in PALETTE)
    return "\n".join(f"  {role:<{width}}  {PALETTE[role]:>3}  {ROLE_DOC[role]}"
                     for role in PALETTE)
if __name__ == "__main__":
    print(f"cfg 'colors' = role -> 256-color index (0-255). Defaults:\n{table()}")
