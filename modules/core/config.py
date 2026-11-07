"""config validation - pure function over the necro.cfg dict.
Unknown keys and wrong types were silently ignored (a typo in 'baseurl' -> the
default kicks in, hard to debug). validate() returns a list of issue strings
(empty = ok); necro.py prints them as startup warnings on stderr."""
from modules.core.palette import ROLES  # valid color roles
KNOWN = {
    "baseurl": str,
    "model": str,
    "swarm": list,
    "remote": bool,
    "reasoning": bool,
    "show_thinking": bool,
    "show_tools": bool,
    "show_edits": bool,
    "max_steps": int,   # int or numeric string (int() is applied at startup)
    "ctx_window": int,  # fallback context window (real size from the inference server)
    "peer_host": str,
    "peer_port": int,   # int or numeric string
    "peer_turn_timeout": int,  # max wall time of a peer turn in seconds
    "step_timeout": int,  # per-step socket timeout in seconds
    "turn_timeout": int,  # wall-clock timeout per turn in seconds
    "max_turns": int,     # max turns per session, self-loop guard
    "telegram_chats": str,
    "telegram_token": str,
    "telegram_format": str,  # 'text' | 'html' | 'markdown' (default markdown)
    "colors": dict,  # role -> 0-255 (roles from core.palette.ROLES)
    "tool_emoji": dict,  # tool -> emoji string (unknown tools fall back to ›)
    "auth": dict,  # optional per-server access tokens {url or host:port: token}
}
def _ok(v, t):
    if t is bool:
        return isinstance(v, bool)  # bool before int: bool is an int subclass
    if t is int:
        return (isinstance(v, int) and not isinstance(v, bool)) or \
            (isinstance(v, str) and v.strip().lstrip("-").isdigit())
    return isinstance(v, t)
def validate(cfg):
    """
    @stable | verified: 2026-11 - frozen: change only on failing test or hard requirement
    Issue strings for an necro.cfg dict: unknown keys (except _doc*), wrong
    types, malformed swarm entries. [] when the config is clean."""
    if not isinstance(cfg, dict):
        return ["config is not a JSON object"]
    issues = []
    for k, v in cfg.items():
        if k.startswith("_doc"):
            continue
        if k not in KNOWN:
            issues.append(f"unknown key '{k}' (ignored)")
            continue
        if not _ok(v, KNOWN[k]):
            issues.append(f"key '{k}' expects {KNOWN[k].__name__}, got {type(v).__name__}")
    swarm = cfg.get("swarm")
    if isinstance(swarm, list):
        for i, a in enumerate(swarm):
            if not isinstance(a, dict) or not isinstance(a.get("model"), str):
                issues.append(f"swarm[{i}] expects {{url?, model}} with model as string")
                continue
            if "url" in a and not isinstance(a["url"], str):
                issues.append(f"swarm[{i}].url expects string, got {type(a['url']).__name__}")
    if "telegram_format" in cfg and cfg["telegram_format"] not in ("text", "html", "markdown"):
        issues.append("telegram_format expects 'text', 'html' or 'markdown'")
    emoji = cfg.get("tool_emoji")
    if isinstance(emoji, dict):
        for tool, e in emoji.items():
            if not isinstance(e, str):
                issues.append(f"tool_emoji['{tool}'] expects string, got {type(e).__name__}")
    colors = cfg.get("colors")
    if isinstance(colors, dict):
        for role, idx in colors.items():
            if role not in ROLES:
                issues.append(f"colors['{role}'] unknown role (valid: {', '.join(sorted(ROLES))})")
            elif not (isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx <= 255):
                issues.append(f"colors['{role}'] expects int 0-255, got {type(idx).__name__}")
    return issues
