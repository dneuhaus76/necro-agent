"""config validation - pure function over the necro.cfg dict.
Unknown keys and wrong types were silently ignored (a typo in 'base_url' -> the
default kicks in, hard to debug). validate() returns a list of issue strings
(empty = ok); necro.py prints them as startup warnings on stderr."""
from modules.core.palette import ROLES, is_index  # valid roles, 256-color range
KNOWN = {
    # endpoint
    "base_url": str,   # OpenAI-compatible inference server URL (env NECRO_BASE_URL)
    "model": str,
    "provider": str,   # runner of the main endpoint (openai|google|deepseek|bigmoe), optional
    "auth": dict,      # optional per-server access tokens {url or host:port: token}
    # swarm + peering
    "swarm": list,     # [{url, model, provider?}]
    "remote": bool,    # master switch: swarm list + peer API
    "peer_api": bool,  # peer API / peer tool only (narrower than remote)
    "peer_host": str,
    "peer_port": int,
    "peer_turn_timeout_s": int,  # max wall time of one peer turn (seconds)
    "peer_max_hops": int,        # max peer hops in one chain (1 = no multi-hop)
    "peer_turn_subagents": bool, # delegate inside peer turns (consult always on)
    # agent loop guard rails
    "turn_max_steps": int,      # model requests inside one turn
    "session_max_turns": int,   # turns per session (self-loop guard)
    "turn_timeout_s": int,      # wall-clock timeout per turn (seconds)
    "step_timeout_s": int,      # socket timeout per model request (seconds)
    "subagent_delegate": bool,  # delegate inside sub-agents (recursion toggle)
    "subagent_max_depth": int,  # max nesting depth of a delegate chain
    "max_output_tokens": int,   # output-token cap when reasoning is off (0 = off)
    # context window
    "ctx_fallback": int,    # window in tokens when the server reports none
    "ctx_models": dict,     # per-model window cap {model: tokens}
    "reasoning": bool,
    # output / UI
    "show_thinking": bool,
    "show_tools": bool,
    "show_edits": bool,
    "colors": dict,     # role -> 256-color index 0-255 (roles: core.palette.ROLES)
    "tool_emoji": dict, # tool -> emoji string (unknown tools fall back to ▸)
    # telegram
    "telegram_chats": str,
    "telegram_token": str,
    "telegram_format": str,  # 'text' | 'html' | 'markdown' (default markdown)
    "telegram_rich": str,    # 'auto' (default) | 'off': rich table detection (Bot API 10.1)
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
        if k.startswith("_"):  # _doc/_note keys carry comments, not settings
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
            if "provider" in a and not isinstance(a["provider"], str):
                issues.append(f"swarm[{i}].provider expects string, got {type(a['provider']).__name__}")
    if "telegram_format" in cfg and cfg["telegram_format"] not in ("text", "html", "markdown"):
        issues.append("telegram_format expects 'text', 'html' or 'markdown'")
    if "telegram_rich" in cfg and cfg["telegram_rich"] not in ("auto", "off"):
        issues.append("telegram_rich expects 'auto' or 'off'")
    emoji = cfg.get("tool_emoji")
    if isinstance(emoji, dict):
        for tool, e in emoji.items():
            if tool.startswith("_"):  # comments
                continue
            if not isinstance(e, str):
                issues.append(f"tool_emoji['{tool}'] expects string, got {type(e).__name__}")
    colors = cfg.get("colors")
    if isinstance(colors, dict):
        for role, value in colors.items():
            if role.startswith("_"):  # _doc/_note keys carry comments, not values
                continue
            if role not in ROLES:
                issues.append(f"colors['{role}'] unknown role (valid: {', '.join(sorted(ROLES))})")
            elif not is_index(value):
                issues.append(f"colors['{role}'] expects a 256-color index 0-255, got {value!r}")
    ctxm = cfg.get("ctx_models")
    if isinstance(ctxm, dict):
        for name, v in ctxm.items():
            if name.startswith("_"):  # _doc/_note keys carry comments, not values
                continue
            if not _ok(v, int) or int(v) <= 0:
                issues.append(f"ctx_models['{name}'] expects a positive int (tokens), got {v!r}")
    return issues
