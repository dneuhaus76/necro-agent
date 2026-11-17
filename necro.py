#!/usr/bin/env python3
"""NECRO - minimal terminal agent harness (Python 3 stdlib only, Debian 13)."""
import codecs, json, os, re, select, signal, socket
import sys, termios, threading, time, tty
from pathlib import Path
VERSION = "0.1.044"  # format: MAJOR.MINOR.PATCH (PATCH 3-stellig, 0-gepaddet)
def load_cfg():
    """necro.cfg (JSON) from cwd, then from the script dir; env vars take precedence."""
    for d in (Path.cwd(), Path(__file__).resolve().parent):
        p = d / "necro.cfg"
        if p.is_file():
            try:
                data = json.loads(p.read_text())
            except ValueError as e:
                print(f"necro: necro.cfg invalid: {e}", file=sys.stderr)
                continue
            if not isinstance(data, dict):  # a JSON list/scalar would break CFG.get() later
                print(f"necro: necro.cfg must be a JSON object: {p}", file=sys.stderr)
                continue
            return str(p), data
    return None, {}
CFG_PATH, CFG = load_cfg()
# config validation - unknown keys / wrong types used to be silently ignored
# (a typo in 'base_url' -> the default kicks in, hard to debug): warn at startup
from modules.core.config import validate as cfg_validate  # noqa: E402
for _issue in cfg_validate(CFG):
    print(f"necro: config: {_issue}", file=sys.stderr)
BASE_URL = os.environ.get("NECRO_BASE_URL", CFG.get("base_url", "http://127.0.0.1:8080/v1"))
MODEL = os.environ.get("NECRO_MODEL", CFG.get("model", "gemma-4-E2B-it"))
def _int(env, cfg_key, default):
    """Int setting: env wins over cfg; fail fast with a precise message on bad values."""
    v = os.environ.get(env)
    if v is None:
        v = CFG.get(cfg_key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        sys.exit(f"necro: {env} (or cfg '{cfg_key}') is not an integer: {v!r}")

TURN_MAX_STEPS = _int("NECRO_TURN_MAX_STEPS", "turn_max_steps", 25)  # model requests per turn
TG_MILESTONE_EVERY = 20  # telegram progress milestone: one message every N steps (was 5)
# safety breaks for unattended operation (self-loop without user intervention):
# per-turn wall-clock timeout (100 steps x long tool calls could otherwise run for hours)
# and a per-session turn cap (the agent can queue its own next prompt via bash/tmux or
# Telegram - without a cap the self-loop never ends). 0 = off.
TURN_TIMEOUT_S = _int("NECRO_TURN_TIMEOUT_S", "turn_timeout_s", 3600)  # s per turn
SESSION_MAX_TURNS = _int("NECRO_SESSION_MAX_TURNS", "session_max_turns", 200)  # turns per session
CTX_FALLBACK = _int("NECRO_CTX_FALLBACK", "ctx_fallback", 8192)  # fallback; real size from inference server
# per-model context cap: on a weak server the real limit is prefill time, not the
# window (131k ctx at 100 tok/s = 20 min per step). cfg 'ctx_models' = {model: tokens}
_cfg_ctx_models = CFG.get("ctx_models")
CTX_MODELS = _cfg_ctx_models if isinstance(_cfg_ctx_models, dict) else {}
CTX_CATCH_ALL = "*"  # ctx_models key for every model without a specific entry
def _cap_norm(name):
    """Model names reach us as 'Qwen3-8-Q8_0.gguf', 'Qwen3.8-27B-Swift' or
    'Qwen3_8': separators and case differ between config and server, the model
    does not. Normalising both sides makes one entry hit all spellings."""
    return re.sub(r"[-._\s]+", "-", str(name).strip().lower())
_cap_keys = {}  # normalised key -> original key (built lazily, CTX_MODELS may change)
def ctx_cap_key(model):
    """The 'ctx_models' key that applies to 'model', or None. Order: exact match,
    then the longest key that prefixes the model id ('qwen3-8' -> 'qwen3-8-...gguf'),
    then the catch-all '*'. Keys starting with '_' are cfg comments, not entries."""
    global _cap_keys
    entries = {k: v for k, v in CTX_MODELS.items() if isinstance(k, str) and not k.startswith("_")}
    if not entries:
        return None
    _cap_keys = {_cap_norm(k): k for k in entries}
    m = _cap_norm(model)
    if m in _cap_keys:
        return _cap_keys[m]
    prefix = max((k for k in _cap_keys if m.startswith(k)), key=len, default=None)
    if prefix is not None:
        return _cap_keys[prefix]
    return CTX_CATCH_ALL if CTX_CATCH_ALL in entries else None
def ctx_cap(model, ctx):
    """Context window to use for 'model': the cfg cap from 'ctx_models' when one
    matches, else the server-reported 'ctx'. Matching is separator/case tolerant
    (see ctx_cap_key); a cap never raises the window above the server value.
    Invalid values (not an int, <= 0) are ignored - never cap to nothing."""
    key = ctx_cap_key(model)
    if key is None:
        return ctx
    try:
        v = int(CTX_MODELS[key])
    except (TypeError, ValueError):
        return ctx
    if v <= 0:
        return ctx
    return min(v, ctx) if ctx > 0 else v  # the server window stays the hard limit
def num(n):
    """Thousands separator as used in de_CH: 12'345 (plain ASCII, TG-safe)."""
    return f"{int(n):,}".replace(",", "'")
# Peer-API: bind address + port, configurable (Docker: host 0.0.0.0 + fixed port)
PEER_HOST = os.environ.get("NECRO_PEER_HOST", CFG.get("peer_host", "127.0.0.1"))
PEER_PORT = _int("NECRO_PEER_PORT", "peer_port", 0)  # 0 = ephemeral
# max wall time of a peer turn (watchdog + HTTP waits) - a busy peer with long
# tool calls must not be aborted at the hard-coded 600 s
PEER_TURN_TIMEOUT_S = _int("NECRO_PEER_TURN_TIMEOUT_S", "peer_turn_timeout_s", 600)
# multi-hop: max peer hops in one chain (hops=1 = direct call from a local
# turn; hops=2 = a peer calling another peer). 1 = old behaviour (no multi-hop).
# default 1 (safety first) - set 2+ for parent-child chains (claims apply).
PEER_MAX_HOPS = _int("NECRO_PEER_MAX_HOPS", "peer_max_hops", 1)
# per-step socket timeout - a hung stream (server alive, no data) used to block
# the turn up to the hard-coded 900 s; now configurable (cfg 'step_timeout_s' / env)
STEP_TIMEOUT_S = _int("NECRO_STEP_TIMEOUT_S", "step_timeout_s", 300)
# remote default - remote (cfg swarm + peer-API) OFF; startup parameter, env wins over cfg.
# the SUBAGENT ('self') flag is gone - see swarm.find().
def _flag(env, cfg_key, default):
    v = os.environ.get(env)
    if v is None:
        v = CFG.get(cfg_key)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "off", "false", "")
REMOTE = _flag("NECRO_REMOTE", "remote", False)
# PEER_API: own switch for the Peer-API/peer tool (like SWARM has NECRO_SWARM) -
# REMOTE stays the master switch (cfg swarm + peer), PEER_API is the narrow one.
# The cfg swarm list is NOT activated by PEER_API (only by REMOTE / NECRO_SWARM).
PEER_API = _flag("NECRO_PEER_API", "peer_api", False)
# swarm delegation (delegate) inside peer turns - default OFF: a peer that only
# owes you an opinion must not start a third server while your terminal blocks.
# consult stays available in peer turns regardless (bounded, no tools, cheap) -
# a second opinion is what a peer turn usually needs.
PEER_TURN_SUBAGENTS = _flag("NECRO_PEER_TURN_SUBAGENTS", "peer_turn_subagents", False)
# delegate/consult inside sub-agents - default OFF (the old SUB_TOOLS lock: no
# unbounded recursion / cost explosion). Depth cap: max nesting levels (explicit
# limit instead of trusting the 10-step loop budget). consult rides on the same
# switch - a stuck sub-agent needs an opinion, not another work loop.
SUBAGENT_DELEGATE = _flag("NECRO_SUBAGENT_DELEGATE", "subagent_delegate", False)
SUBAGENT_MAX_DEPTH = _int("NECRO_SUBAGENT_MAX_DEPTH", "subagent_max_depth", 1)
# "think less" per request - when reasoning is OFF, cap the output tokens
# (best effort: servers differ in how they count reasoning tokens). 0 = off.
MAX_OUTPUT_TOKENS = _int("NECRO_MAX_OUTPUT_TOKENS", "max_output_tokens", 0)
REMOTE_CFG = REMOTE  # swarm resolution uses the original value (PEER_API adds no swarm)
REMOTE = REMOTE or PEER_API
# chat whitelist (env NECRO_TELEGRAM_CHATS or cfg 'telegram_chats', comma-separated
# ids; empty = nobody, deny-all) so strangers cannot spend tokens - SSOT for tg_submit + greeting
TG_CHATS = [c for c in (os.environ.get("NECRO_TELEGRAM_CHATS") or CFG.get("telegram_chats") or "").replace(" ", "").split(",") if c]
def tg_command(text):
    """Split a Telegram message into (command, argument). A command starts with '/';
    the client appends '@botname' in groups ('/model@necro_bot x'), which is not part
    of the command. A plain prompt returns ('', text)."""
    if not text.startswith("/"):
        return "", text
    head, _, rest = text.partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


# the commands a Telegram user can send (shown by /help). Anything else is a prompt.
# SSOT: the /help text is derived from this list, and the same list is pushed to
# Telegram (setMyCommands) so the client shows them in its own command menu.
# Descriptions: complete words, short enough for the narrow column in the
# Telegram client menu. The API allows 256 chars, but a long text is cut by the
# CLIENT - and a cut word looks like a typo ("interrupt the current tur").
# test_tg_commands.py holds the ceiling and rejects a mid-word cut.
TG_COMMAND_SPECS = (
    ("stop", "abort the running turn"),
    ("new", "new session, reset context"),
    ("model", "show details / switch model"),
    ("compact", "shrink the context now"),
    ("status", "context and token usage"),
    ("tgcommands", "update or remove this menu"),
    ("quit", "quit NECRO"),
    ("help", "this list"),
)
# the sub-commands of /tgcommands (the only command that takes a word, not a value)
TG_MENU_CMDS = ("update", "remove")
# every command the Telegram side answers itself - anything else starting with '/'
# is a typo (a prompt would cost a turn and confuse the user with an agent reply)
TG_KNOWN = frozenset({"/" + name for name, _ in TG_COMMAND_SPECS} | {"/commands"})
TG_COMMANDS = (
    "Commands from Telegram:\n"
    + "".join(f"  /{name:<14} - {desc}\n" for name, desc in TG_COMMAND_SPECS)
    + "  <Text>         - normal prompt (starts a turn)"
)
# read-only queries: answered immediately, even while a turn runs - they only read
# state (tokens, help text), so queueing them behind a tool call would just be stale.
TG_INSTANT_CMDS = ("/help", "/status")
# the TUI key + command reference (shown by /help in the prompt)
HELP_TUI = (
    "Keys:\n"
    "  ^t thinking · ^o tools · ^x edits\n"
    "  ↑/↓ history · PgUp/PgDn scroll · Alt+Enter newline\n"
    "  TAB complete (/commands) · Esc abort / close menu · ^c clear (idle) / abort (busy)\n"
    "Commands:\n"
    "  /help            - this reference\n"
    "  /model [name]    - model details / switch model\n"
    "  /compact         - shrink the context (summarise the old part)\n"
    "  /status          - context/token usage (real or labelled estimate)\n"
    "  /tgcommands      - Telegram menu: update | remove\n"
    "  /cd <dir>        - change working directory\n"
    "  /clear|/new      - new session (reset context)\n"
    "  /review /test /plan - prompt templates\n"
    "  /quit            - quit NECRO\n"
    "  <Text>           - normal prompt (starts a turn)"
)
# swarm: list of {url, model} agents the main model can delegate tasks to (model name is the identifier).
# OFF by default - active via NECRO_SWARM ("model=url;model=url", empty = off) or REMOTE (cfg list).
# pure logic in modules/io/swarm.py (parameterized, unit-testable); globals stay here.
from modules.io.swarm import (agents_str as swarm_agents_str, check_loop as swarm_check_loop,
                           resolve as swarm_resolve)
SWARM = swarm_resolve(CFG, os.environ.get("NECRO_SWARM"), REMOTE_CFG, BASE_URL, MODEL)
# RULES = behavioural rules shared by the main prompt and every worker prompt (SSOT);
# skills_hint = the skills index line. Both belong in the MAIN prompt too - a hint
# only the worker gets is a dead feature for a project without NECRO.md.
from modules.ui.subagent import RULES, skills_hint
# central palette - apply cfg 'colors' overrides at startup (rebuilds the SGR constants),
# then bind the color names (imported AFTER set_palette so they reflect the overrides).
from modules.core import palette as _palette
from modules.ui import render as _render
_render.set_palette(_palette.resolve(CFG.get("colors")))
# standard marker: user input + input prompt
MARK_USER = "›"
# fallback marker for everything non-user (unknown tool heads)
MARK_TOOL = "▸"
# tool emojis - cfg 'tool_emoji' {tool: emoji} overrides the defaults
TOOL_EMOJI = dict(_palette.EMOJI)
if isinstance(CFG.get("tool_emoji"), dict):
    TOOL_EMOJI.update({k: v for k, v in CFG["tool_emoji"].items() if isinstance(v, str)})
PEER_TAG = TOOL_EMOJI["peer"] + " peer"  # peer-line prefix (detection + Peer-API); default via SSOT (palette.EMOJI), cfg 'tool_emoji' overrides
from modules.ui.render import MUTED, TELEGRAM, ACCENT  # noqa: E402
# DB lives in data/ (WAL mode creates -wal/-shm side files that would clutter the project root)
DB_PATH = Path(os.environ.get("NECRO_DB", str(Path(__file__).resolve().parent / "data" / "necro.db")))  # per instance override
# inference-server probes extracted to modules/core/probe.py (pure functions of base+model)
from modules.core import inference, probe, providers
from modules.core import agent as agent_mod  # agent loop (inference + tool execution)
from modules.io.tools import CONTENT_CAP  # SSOT: MD context load cap
from modules.core.templates import TEMPLATES, expand as template_expand  # /review /test /plan
# cfg 'reasoning': true/false = explicit level (sent to the server as the 'thinking' bool);
# absent = auto-detect from the inference server. REASONING is the request-level toggle;
# app.reasoning_effort is the server-reported effort string for display (a different
# concern, so the two are kept separate). Resolved in the background (non-blocking
# startup): fallback values are used until the probe results arrive; the App instance
# is notified to redraw.
# explicit provider name (cfg 'provider' / env NECRO_PROVIDER) - the direct way to
# name a runner whose hostname gives no hint (local IPs, e.g. BigMoeOnEdge); swarm
# agents carry their own cfg 'swarm' entry 'provider'. None = hostname decides.
PROVIDER_NAME = providers.provider_name(CFG)
_cfg_reason = CFG.get("reasoning")
REASONING = _cfg_reason if isinstance(_cfg_reason, bool) else False  # startup default; live value on the App
def _resolve_model_info(app):
    """Model probe (background): fills app.ctx / app.reasoning_effort / app.model_info
    and marks the UI dirty when the results arrive."""
    def on_done(info, think):
        # the cfg cap wins over the server window: a slow server's real limit is prefill time
        app.set_ctx(info["ctx"])  # cfg cap wins, and the active key is remembered
        app.reasoning_effort, app.model_info = info["effort"], info
        app.ctx_raw = info["ctx"]  # keep the uncapped server value for later /model switches
        app.model_info["ctx"] = app.ctx  # /model shows the effective window, not the raw server value
        if not isinstance(_cfg_reason, bool):
            app.reasoning = think
        app.dirty = True
    probe.resolve(_cfg_reason, BASE_URL, app.model, app.ctx, on_done,
                  token=inference.token_for(BASE_URL, CFG.get("auth")),  # optional per-server token
                  name=PROVIDER_NAME)
# swarm availability: background check every 30 s (GET /models, 3 s timeout); the header
# shows on/off per agent so an offline swarm is visible before a delegate call fails.
# mutable on purpose (filled by the background check, read by view + agent):
# the shared dict is the live source, so it is not a CONSTANT.
swarm_up = {}


def _swarm_check(app):
    """Swarm availability loop (background): marks the UI dirty on status changes."""
    def on_change(up):
        swarm_up.clear()
        swarm_up.update(up)
        app.dirty = True
    swarm_check_loop(SWARM, on_change)
def start_background(app):
    """Daemon threads (model probe, swarm availability) - started in main(),
    not at import time, so importing necro has no side effects."""
    threading.Thread(target=_resolve_model_info, args=(app,), daemon=True).start()
    if SWARM:
        threading.Thread(target=_swarm_check, args=(app,), daemon=True).start()
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
CURSOR_UNDERLINE = b"\x1b[4 q"  # DECSCUSR steady underline '_' (non-blinking) instead of the default block
CURSOR_DEFAULT = b"\x1b[0 q"  # restore the terminal's default cursor shape on exit
def _load_md(filename):
    """Nearest file (cwd, then script dir); name may use ~ or $VAR; (None, "") if absent."""
    name = os.path.expandvars(os.path.expanduser(filename))
    for d in (Path.cwd(), Path(__file__).resolve().parent):
        p = d / name  # an absolute name overrides d, so both dirs hit the same file
        if p.is_file():
            t = os.path.expandvars(p.read_text(errors="replace").strip())
            return str(p), (t[:CONTENT_CAP] + "\n…[truncated]") if len(t) > CONTENT_CAP else t
    return None, ""
# NECRO.md takes precedence: if it exists it IS the project context. In that case
# AGENTS.md is intentionally NOT loaded (SYSTEM = base + NECRO.md only).
NECRO_MD_PATH, NECRO_MD = _load_md("NECRO.md")
AGENTS_MD_PATH, AGENTS_MD = _load_md("AGENTS.md")
# the project context: NECRO.md wins, AGENTS.md is the fallback. ONE tuple for
# both the main prompt and the sub-agent prompts - a worker that reads a different
# project document than its caller follows different rules.
CONTEXT_MD_PATH, CONTEXT_MD = ((NECRO_MD_PATH, NECRO_MD) if NECRO_MD
                               else (AGENTS_MD_PATH, AGENTS_MD))
# pi-style behavioural rules (RULES, modules/ui/subagent.py) steer every model -
# main and worker - into proven tool usage (search with grep not cat; small unique
# edit anchors; offset/limit for large reads). One SSOT: a rule the worker does
# not see is not enforced where the editing happens.
# STATIC on purpose: no cwd, no date, no tool list. A system prefix that changes
# per import kills the server prefix cache, and the module scope cannot know the
# cwd after /cd or the tool set after the SWARM/REMOTE toggles. Those session
# facts are injected once by system_prompt() at App start (see App.__init__).
SYSTEM = "You are NECRO, a minimal agent in a terminal. " + RULES
if _skills := skills_hint():  # static per install -> belongs in the cacheable prefix
    SYSTEM += " " + _skills
if CONTEXT_MD:
    SYSTEM += f"\n\nProject instructions ({CONTEXT_MD_PATH}):\n{CONTEXT_MD}"
from modules.io.tools import (BASH_TIMEOUT, TOOL_OUT_CAP, GREP_MAX_COUNT,  # SSOT: bash timeout, output cap, grep match cap
                              LOOK_MAX_BYTES)  # SSOT: look size limit (tool description)
def tool(name, desc, opt=(), types=None, **props):
    types = types or {}
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "required": [k for k in props if k not in opt],
        "properties": {k: {"type": types.get(k, "string"), "description": v} for k, v in props.items()}}}}
TOOLS = [
    tool("bash", f"Run a shell command ({BASH_TIMEOUT} s timeout). Output capped at {TOOL_OUT_CAP} chars. "
                 "stderr merged into stdout. Each call starts in the process working dir (cd does not persist between calls).",
                 command="Shell command"),
    tool("read_file", "Read a text file, optionally a line range.", opt=("offset", "limit"),
         types={"offset": "integer", "limit": "integer"},
         path="File path", offset="First line, 1-based (default 1)", limit="Max lines (default: all)"),
    tool("write_file", "Write (overwrite) a file, creating parent directories as needed.",
         path="File path", content="Full content"),
    tool("edit_file", "Replace exactly one occurrence of old with new.",
         path="File path", old="Text to replace", new="Replacement"),
    tool("grep", f"Search files with a regex (ripgrep); returns 'file:line: text' (dir) or 'line: text' (single file), "
                 f"max {GREP_MAX_COUNT} matches per file. Cheaper than read_file.",
         opt=("path",), pattern="Regex pattern", path="File or directory (default: cwd)"),
    tool("recall", "Keyword search over PAST sessions (last 7 days): finds earlier user/agent/tool messages "
                   "matching the query (OR semantics, ranked by relevance). Returns '[ts] role: text' hits. "
                   "Use to remember what was done or decided in previous sessions "
                   "('what was done last week?'). Not for searching files - use grep for that.",
         query="Keywords or short phrase to search for"),
    tool("look", "Load an image into the model context (JPEG/PNG/WebP, max "
                 f"{LOOK_MAX_BYTES // (1024 * 1024)} MB). The image is sent to the model for one "
                 "request, then replaced by a text placeholder. Use to view screenshots, diagrams, "
                 "photos. Not for GIF/BMP.",
         path="Image file path"),
]
if SWARM:
    _agents = swarm_agents_str(SWARM)
    TOOLS.append(tool("delegate",
                      "Hand a self-contained task to a swarm agent (separate model on its own server). It runs its own "
                      "tool loop (bash, read_file, ...) in the same working dir and returns the result. It does NOT see "
                      "this conversation - include all needed context in the task. Use for work that can be delegated. "
                      "For a second opinion/expert advice use consult instead. Smaller models (e.g. 2B) are much less "
                      "capable - keep their tasks simple and self-contained. Agents (duplicate names need 'name@host'): " + _agents,
                      model="Target swarm model name", task="Self-contained task description"))
    TOOLS.append(tool("consult",
                      "Bot-to-bot consultation: multi-round dialogue with a swarm agent that may ask up to 3 follow-up "
                      "questions, which you answer (it sees only your answers, not your full context). Use for expert "
                      "opinions, reviews, second opinions. For delegated work with tools use delegate instead. "
                      "Prefer the most capable agent for complex questions. Agents (duplicate names need 'name@host'): " + _agents,
                      model="Target swarm model name", question="Question or topic for the consultation"))
SUB_TOOLS = [t for t in TOOLS if t["function"]["name"] not in ("delegate", "consult", "peer")]  # no recursion / ping-pong
def peer_turn_tools(hops):
    """multi-hop: toolset of a PEER turn (hops = hop number of this turn,
    1 = direct call from a local turn). peer: only while hops < PEER_MAX_HOPS
    (the chain budget in peer_prompt caps the receiver's watchdog). consult: always
    (bounded, no tools). delegate: only when PEER_TURN_SUBAGENTS is on - a peer
    that pulls in a third server blocks the caller for the whole sub-run.
    Local turns are unchanged."""
    names = {t["function"]["name"] for t in TOOLS}
    keep = set(names) - {"delegate", "peer"}
    if hops < PEER_MAX_HOPS:
        keep.add("peer")
    if PEER_TURN_SUBAGENTS:
        keep.add("delegate")
    return [t for t in TOOLS if t["function"]["name"] in keep]
PEER_TOOL = tool("peer", "Delegate a full agent turn to a sibling NECRO instance on this machine "
                  "(own context, own model - it runs the task in its own UI and returns the final reply). "
                  "Use for parallel work or when the sibling has a different model. "
                  "If the peer is busy, your task is queued and runs after the current turn. "
                  "Returns an error if the peer is offline - retry later in that case. Instances: (none)",
                  opt=("instance",), task="Self-contained task description",
                  instance="Sibling instance id (default: first available)")
if REMOTE:  # peer-API / sibling instances are a remote feature - off by default
    TOOLS.append(PEER_TOOL)
def system_prompt():
    """SYSTEM (static rules) + the session facts: cwd, date, live tool names.
    Called at App start and after /cd or /clear so the prompt never states a
    stale working dir, a stale date or a tool that is not wired. The facts sit
    at the END of the prefix (cheap to re-send, the rule block stays cacheable)."""
    names = ", ".join(t["function"]["name"] for t in TOOLS)
    return (f"{SYSTEM}\nWorking dir: {os.getcwd()}. Today: {time.strftime('%Y-%m-%d')}. "
            f"Tools: {names}. Prefer these over guessing paths. "
            f"If you need the exact time, run `date +%F`.")
_peer_refresh_sig = None  # last built peer tool description (change guard, see peer_refresh)
def peer_refresh():
    """Refresh the peer tool description with the current sibling instances (discovery files).
    Each instance shows its busy status (cached, 5 s TTL) - idle peers are
    preferred for routing (see Agent._tool_peer). Grandchildren (claimed
    by my children) stay out of the list - parent-child routing only.
    Guard: peer_refresh() runs every agent step in remote mode - only rebuild
    the description when the instance set / busy states actually changed.
    Rebuilding = glob + per-peer HTTP health + string rebuild, all wasted work
    when nothing moved."""
    global _peer_refresh_sig
    ps = []
    try:
        from modules.io.api import peer_instances, peer_busy, OWN_ID, peer_excluded_ids
        excl = peer_excluded_ids()
        ps = [x for x in peer_instances()  # not myself (PID is not unique across containers)
              if x["id"] != OWN_ID and x["id"] not in excl]
    except Exception as e:
        print(f"necro: peer discovery failed: {type(e).__name__}: {e}", file=sys.stderr)
    def _line(p):
        b = peer_busy(p)  # True/False/None (unknown, 5 s TTL cache in api.py)
        state = "busy" if b else "idle" if b is False else "status unknown"
        # model/url are optional (older instances do not publish them)
        who = f", {p['model']} @ {p.get('url') or '?'}" if p.get("model") else ""
        return f"{p['id']} (pid {p['pid']}, {p['cwd']}, {state}{who})"
    desc = PEER_TOOL["function"]["description"].rsplit("Instances: ", 1)[0] + \
        ("Instances: " + "; ".join(_line(p) for p in ps) if ps
         else "Instances: (none - no sibling NECRO running)")
    # rebuild the description only when it actually changed (instance set or a
    # busy-state flip) - peer_busy() itself is TTL-cached (5 s), so this guard
    # mainly saves the string rebuild + dict write on every unchanged step
    if desc == _peer_refresh_sig:
        return
    _peer_refresh_sig = desc
    PEER_TOOL["function"]["description"] = desc
def peer_status_loop(app):
    """Delayed peer status for the Peer-API line (like the model probe in
    the header): first check after 3 s, then every 30 s; the line is green (ACCENT)
    while a peer is online, neutral (MUTED) otherwise. Redraws only on state change.
    The block is read from app on every iteration - /clear//new replaces it."""
    from modules.io.api import peer_instances, OWN_ID
    last, last_blk = None, None
    time.sleep(3)  # first check after 3 s
    while True:
        try:
            ps = [x for x in peer_instances() if x["id"] != OWN_ID]
        except Exception:
            ps = []
        state = tuple(sorted(x["id"] for x in ps))
        blk = getattr(app, "peer_blk", None)
        if blk is not None and (state != last or blk is not last_blk):
            # color change (peer online/offline) OR a fresh block after /clear//new
            last, last_blk = state, blk
            with app.lock:
                blk.color = ACCENT if state else MUTED
            app.dirty = True
        time.sleep(30)
# ------------------------------------------------------------------------ app
# engine + view are mixins (necro.py stays the composition root: config, tool schemas,
# main loop). Both modules get the values this file owns injected once - see configure().
from modules.core.turn import TurnMixin, configure as _turn_cfg
from modules.ui.view import ViewMixin, configure as _view_cfg
from modules.core.block import Block  # re-export: "from necro import Block" stays valid
_turn_cfg(TURN_MAX_STEPS=TURN_MAX_STEPS, SESSION_MAX_TURNS=SESSION_MAX_TURNS, TURN_TIMEOUT_S=TURN_TIMEOUT_S,
          TG_MILESTONE_EVERY=TG_MILESTONE_EVERY, REMOTE=REMOTE,
          PEER_MAX_HOPS=PEER_MAX_HOPS, PEER_TURN_SUBAGENTS=PEER_TURN_SUBAGENTS,
          peer_refresh=peer_refresh, peer_turn_tools=peer_turn_tools)
_view_cfg(VERSION=VERSION, BASE_URL=BASE_URL, TURN_MAX_STEPS=TURN_MAX_STEPS, SPINNER=SPINNER, MARK_USER=MARK_USER,
          MARK_TOOL=MARK_TOOL, TOOL_EMOJI=TOOL_EMOJI, PEER_TAG=PEER_TAG,
          SWARM=SWARM, swarm_up=swarm_up)
from modules.ui.keys import KeyHandler  # key handling (mixin)


class App(KeyHandler, TurnMixin, ViewMixin):
    def __init__(self):
        self.blocks, self.lock = [], threading.RLock()
        self.messages = [{"role": "system", "content": system_prompt()}]
        self.buf, self.cur, self.rx, self.rx_time = "", 0, "", 0.0
        self.scroll, self.last_tot = 0, 0
        # cfg 'show_thinking'/'show_tools'/'show_edits' set the startup default for the ^t/^o/^x toggles
        self.think = bool(CFG.get("show_thinking", True))
        self.busy, self.quit, self.dirty = False, False, True
        self.step = 0  # current agent step of the running turn (shown in the header, right)
        self.turns = 0  # turns since session start / /clear (per-session cap)
        self.tab_hint, self._tab_cands, self._tab_i = "", None, 0  # TAB completion state
        self._tab_cycle = None  # active cycle list (survives the inserted exact match)
        self.menu, self.menu_sel = None, 0  # horizontal popup menu (TAB candidates)
        self.history, self._hist_i = [], 0  # ↑/↓ prompt history; _hist_i = position (len = present)
        self._queued, self._queued_text, self._queued_block = False, "", None  # queued prompt state
        self._tg_queued = None  # (text, chat_id, sender) queued while busy: runs right after this turn
        self._spin = " "  # last drawn spinner frame; a tick without content change redraws only the header
        self._last_reply = None  # dict of the last assistant message appended in a turn (peer reply lookup)
        self._last_cursor = None  # (abs row, col) of the wanted physical cursor position
        self._last_input_rows = set()  # rows holding input lines (resize cleanup)
        self._last_view = {}  # row -> last written line (incremental draw)
        self._last_wh = None  # (W, H) of the last full draw - a resize forces a full redraw
        self._last_cwd, self._cwd_flash = os.getcwd(), 0.0  # cwd change: flash the status line
        self.show_tools, self.show_edits = bool(CFG.get("show_tools", True)), bool(CFG.get("show_edits", True))
        self.stop, self.conns = threading.Event(), []  # conns: all open sockets (main + swarm sub-requests)
        self.tg = None  # TelegramBot, set in main() when enabled
        self.tg_chats = []  # whitelisted chat ids (greeting + run summary)
        self.peer = None  # PeerAPI, set in main()
        self.peer_tag = PEER_TAG  # peer-line prefix for the Peer-API (modules/io/api.py)
        self.model = MODEL  # live model (switchable via /model); module MODEL is the startup value
        self.reasoning = REASONING  # request-level thinking toggle (probe may update it)
        self.reasoning_effort = ""  # server-reported effort string for display
        self.model_info = probe.default_info(CTX_FALLBACK)  # filled by the background probe
        self.ctx, self.ctx_used = ctx_cap(MODEL, CTX_FALLBACK), 0  # ctx_used: real prompt_tokens
        self.ctx_raw = CTX_FALLBACK  # server-reported window (uncapped) - the base for a /model switch
        self.ctx_capped_key = ctx_cap_key(MODEL)  # active cfg 'ctx_models' entry (None = server value)
        self.last_completion_tokens = 0  # completion tokens of the last stream (turn trace)
        self.ctx_est = len(json.dumps(self.messages, ensure_ascii=False)) // 3  # cached estimate (db_sync refreshes it)
        from modules.core.sessiondb import SessionDB  # turns DB: one row per session, parallel-safe, 7-day pruning
        self.sessions = SessionDB(DB_PATH)
        self.agent = agent_mod.Agent(self, BASE_URL, STEP_TIMEOUT_S, TOOLS, SUB_TOOLS, SWARM, swarm_up,
                                     CONTEXT_MD, CONTEXT_MD_PATH, auth=CFG.get("auth"),
                                     subagent_delegate=SUBAGENT_DELEGATE,
                                     subagent_max_depth=SUBAGENT_MAX_DEPTH,
                                     max_output_tokens=MAX_OUTPUT_TOKENS,
                                     provider=PROVIDER_NAME,
                                     peer_max_hops=PEER_MAX_HOPS)  # agent loop (pure logic)
    # ---- db
    def _note_cwd(self):
        """Detect a working-dir change (e.g. via a bash tool call) and flash the status line."""
        cwd = os.getcwd()
        if cwd != self._last_cwd:
            self._last_cwd, self._cwd_flash = cwd, time.time()
            global _GIT_BRANCH
            _GIT_BRANCH = None  # re-resolve the branch for the new directory
            # the system prompt names the working dir - keep it true after a bash cd
            if self.messages and self.messages[0].get("role") == "system":
                self.messages[0] = {"role": "system", "content": system_prompt()}
            self.dirty = True
    def db_sync(self):
        """Mirror the active context (all messages except system) into the turns table
        and refresh the cached context-size estimate (draw() must stay allocation-free)."""
        with self.lock:
            if self.sessions.closed:  # shutdown: a straggler thread must not raise
                return
            self.sessions.sync(self.messages)
            try:
                self.ctx_est = len(json.dumps(self.messages, ensure_ascii=False)) // 3
            except Exception:
                self.ctx_est = 0
    # ---- blocks
    def add(self, kind, text="", color=None):
        b = Block(kind, text, color)
        with self.lock:
            self.blocks.append(b)
        self.dirty = True
        return b
    def append(self, b, text):
        with self.lock:
            b.text += text
        self.dirty = True
    def set_head(self, b, head):
        """replace the block's first line (live progress header), keep the body."""
        with self.lock:
            rest = b.text.split("\n", 1)[1] if "\n" in b.text else ""
            b.text = head + ("\n" + rest if rest else "")
        self.dirty = True
    # ---- input
    def ins(self, t):
        self.buf = self.buf[:self.cur] + t + self.buf[self.cur:]
        self.cur += len(t)
        self.tab_hint, self.menu, self._tab_cycle = "", None, None  # typing closes the popup
    def cancel_queued(self):
        """Drop the queued prompt (block + text) - Enter on empty input while busy."""
        self._queued = False
        self._queued_text = ""
        if self._queued_block is not None:
            with self.lock:
                try:
                    self.blocks.remove(self._queued_block)
                except ValueError:
                    pass
            self._queued_block = None
        self.dirty = True
    def set_ctx(self, raw):
        """Effective context window for a raw server value, plus the cfg 'ctx_models'
        key that limited it (None = server value). Status line and /status show that
        key, so a configured cap that never matches is visible instead of silent."""
        self.ctx_capped_key = ctx_cap_key(self.model)
        self.ctx = ctx_cap(self.model, raw)
        return self.ctx
    def status_text(self):
        """Text for /status - TUI info block and Telegram reply share it, so the
        numbers can never drift apart. Token source: real prompt_tokens of the last
        request; before the first request (or after a reset) a labelled estimate."""
        used, src = self.ctx_used, "real, last request"
        if not used:
            used, src = self.ctx_est, "estimated"
        pct = 100.0 * used / self.ctx if self.ctx else 0.0
        state = f"busy (step {self.step})" if self.busy else "idle"
        cap = self.ctx_capped_key
        lines = [
            f"status: {state} | turn {self.turns}",
            f"model: {self.model} (ctx {self.ctx // 1000}k, "
            + (f"cap ctx_models['{cap}']" if cap else "server value") + ")",
            f"tokens: {num(used)} / {num(self.ctx)} ({pct:.1f}%, {src})",
            f"free: {num(max(self.ctx - used, 0))} tokens",
        ]
        if self.last_completion_tokens:
            lines.append(f"last answer: {num(self.last_completion_tokens)} tokens")
        lines.append(f"cwd: {os.getcwd()}")
        return "\n".join(lines)
    def model_info_text(self):
        """Human-readable model details for the /model command. The provider profile
        decides which rows make sense (a hosted API has no GPU layers/threads).
        A cfg 'ctx_models' cap is named next to the ctx row: the effective window
        differs from the server's --ctx-size, and an unexplained number looks wrong."""
        prof = providers.resolve(BASE_URL, PROVIDER_NAME)
        info = dict(self.model_info)  # copy: the cap note is display-only
        if cap := getattr(self, "ctx_capped_key", None):
            info["ctx"] = f"{info['ctx']} (cap ctx_models['{cap}'])"
        return probe.info_text(self.model, info, prof)
    def compact_command(self, chat_id=None):
        """/compact: run the compaction and report the result. It makes one LLM
        call, so it runs in the background - the TUI is a single-threaded loop
        and must not freeze, and the Telegram poller must not stall either.
        chat_id = None: report as a TUI info block; else send it to that chat."""
        def work():
            try:
                result = self.agent.compact(force=True) or "compact: nothing to do (context is small)"
            except Exception as e:  # a failed summary must not kill the app
                result = f"compact failed: {type(e).__name__}: {e}"
            if chat_id is None:
                self.add("info", result)
            else:
                self.tg.send(chat_id, result)
        threading.Thread(target=work, daemon=True).start()

    def tg_menu(self, arg):
        """Telegram /tgcommands: update ('' or 'update') pushes the command menu,
        'remove' (or the legacy 'clear') clears it. Returns the reply text."""
        if self.tg is None:
            return "tgcommands: Telegram is off (start with --telegram / NECRO_TELEGRAM=1)"
        sub = (arg or "update").lower()
        if sub not in TG_MENU_CMDS + ("clear",):
            return f"tgcommands: unknown '{arg}'\n  /tgcommands update - push the menu\n  /tgcommands remove - clear the menu"
        if sub == "update":
            if self.tg.set_my_commands(TG_COMMAND_SPECS):
                return f"tgcommands: menu updated ({len(TG_COMMAND_SPECS)} commands)"
            return "tgcommands: setMyCommands failed (check the token)"
        if self.tg.clear_my_commands():
            return "tgcommands: menu cleared"
        return "tgcommands: setMyCommands failed (check the token)"

    def model_command(self, arg):
        """One implementation for the TUI /model and the Telegram /model: no arg
        shows the details, a name switches the live model. Returns the reply text
        (the TUI adds it as an info block, Telegram sends it)."""
        if not arg:
            return self.model_info_text()
        self.model = arg
        self.set_ctx(self.ctx_raw)  # cfg cap applies before the probe answers
        self.model_info = probe.default_info(self.ctx)  # reset to empty (all keys) until the re-probe lands
        threading.Thread(target=_resolve_model_info, args=(self,), daemon=True).start()  # re-probe the new model
        if self.peer is not None:  # keep the published model in the discovery file current
            try:
                self.peer.announce(model=arg)
            except OSError as e:
                return (f"model: {arg} (probe laeuft …)\n"
                        f"peer discovery not updated: {type(e).__name__}: {e}")
        return f"model: {arg} (probe laeuft …)"

    def _start_turn(self, display, content, chat_id=None):
        """Common turn start: user block + context append + background turn thread.
        display may differ from content (Telegram shows the [telegram:sender] prefix)."""
        self.add("user", display)
        self.messages.append({"role": "user", "content": content})
        self.db_sync()
        self.scroll, self.busy = 0, True
        self.stop.clear()
        if chat_id is not None:  # telegram: the bot is busy now (cleared with 'done' at turn end)
            self.tg.send(chat_id, "🕸 busy")
        threading.Thread(target=self.turn, args=() if chat_id is None else (chat_id,), daemon=True).start()
    @staticmethod
    def _is_turn_text(t):
        """Does this text start a turn (increment self.turns)? Mirrors the command
        dispatch in submit(): /help, /quit, /clear//new, /model, /tgcommands and
        /cd are NOT turns; prompt templates and plain text ARE turns (both go
        through _start_turn).
        Note: submit() only treats '/model' or '/model <arg>' as a command -
        '/modelFoo' falls through to _start_turn there, so this must NOT match
        it either (a turn that is misclassified here skips the 5 s wait-ast)."""
        if t in ("/help", "/quit", "/clear", "/new", "/status", "/compact") \
                or t == "/model" or t.startswith("/model ") \
                or t == "/tgcommands" or t.startswith("/tgcommands ") \
                or t.startswith("/cd "):
            return False
        return True
    def clear_session(self):
        """New session: reset blocks + context (shared by TUI /clear, TG /new, Ctrl-C)."""
        with self.lock:
            self.blocks.clear()
        self.messages, self.scroll, self.ctx_used, self.turns = self.messages[:1], 0, 0, 0
        if self.messages:  # refresh the session facts (date may have rolled over)
            self.messages[0] = {"role": "system", "content": system_prompt()}
        self.db_sync()
        try:
            self.sessions.prune()  # retention for long runners (open-time prune never repeats)
        except Exception:
            pass  # a pruning failure must not break /clear
        startup_info(self)  # main info (emblem + status lines) like at startup
    def submit(self, t=None):
        # t: explicit text (queued prompt after a turn) - the input line was
        # already cleared at queue time, so buf is only the source for typed text
        if t is None:
            t = self.buf.strip()
        if not t:
            if self.busy and self._queued:  # Enter on empty input cancels the queued prompt
                self.cancel_queued()
            return
        if t == "/quit":  # quit also mid-turn: an explicit quit intent must not wait for a tool call
            if self.busy:
                self.abort()
                self.add("info", "abort: laufender Turn wird beendet …")
            self.quit = True
            return
        if t in TG_INSTANT_CMDS:  # read-only queries answered NOW, also while busy (no queue wait)
            self.history = (self.history + [t])[-100:]  # still recallable via arrows
            self._hist_i = len(self.history)
            self.buf, self.cur, self.tab_hint, self.menu = "", 0, "", None
            self.add("info", HELP_TUI if t == "/help" else self.status_text())
            return
        if self.busy:  # queue - prompt moves into a queued block, input line stays free
            if self._queued:  # a queued prompt already pending: replace it
                self.cancel_queued()
            self._queued_text = t
            self._queued_block = self.add("user", f"(Queued - runs after the current turn)\n{t}")
            self._queued = True  # Enter was pressed: resubmit after the turn (F2)
            self.buf, self.cur, self.tab_hint, self.menu = "", 0, "", None
            return
        self.history = (self.history + [t])[-100:]
        self._hist_i = len(self.history)
        self.buf, self.cur, self.tab_hint, self.menu = "", 0, "", None
        if t in ("/clear", "/new"):  # /new = /clear (same context reset as Telegram /new)
            self.clear_session()
        elif t == "/model" or t.startswith("/model "):  # /model = details, /model <name> = switch
            self.add("info", self.model_command(t.partition(" ")[2].strip()))
        elif t in TEMPLATES or any(t.startswith(c + " ") for c in TEMPLATES):  # prompt template
            cmd, _, rest = t.partition(" ")
            t = template_expand(cmd, rest)  # expanded prompt (template + focus text)
            self._start_turn(t, t)
            return
        elif t == "/compact":  # shrink the context now (background: it makes one LLM call)
            self.compact_command()
        elif t == "/tgcommands" or t.startswith("/tgcommands "):  # push/clear the TG menu
            self.add("info", self.tg_menu(t.partition(" ")[2].strip()))
        elif t.startswith("/cd "):  # change the working directory (shown in the status line)
            d = Path(t[4:]).expanduser()
            if not d.is_dir():
                self.add("info", f"cd: no such directory: {d}")
            else:
                os.chdir(d)
                self._note_cwd()  # flash + branch cache invalidation
                self.messages[0] = {"role": "system", "content": system_prompt()}  # prompt states the cwd
                self.add("info", f"cd: {os.getcwd()}")
        else:
            self._start_turn(t, t)
    def tg_submit(self, text, chat_id, sender=""):
        """Telegram message: same as a typed turn, but the final agent reply goes back to the chat."""
        whitelisted = str(chat_id) in TG_CHATS  # empty whitelist = nobody (deny-all)
        if not whitelisted:
            self.tg.send(chat_id, f"(unknown chat {chat_id} - not in the whitelist)")
            return
        cmd, arg = tg_command(text)  # '/model@necro_bot x' -> ('/model', 'x')
        if cmd == "/stop":  # only /stop interrupts (not bare "stop")
            if self.busy:
                self.abort()
                self.tg.send(chat_id, "stop: unterbreche aktuellen Turn …")
            else:
                self.tg.send(chat_id, "stop: no active turn")
            return
        if cmd in ("/help", "/commands") or text == "help":  # list the Telegram commands
            self.tg.send(chat_id, TG_COMMANDS)
            return
        if cmd == "/new":  # new session (same reset as the TUI /clear)
            self.clear_session()
            self.tg.send(chat_id, "new: new session (context reset)")
            return
        if cmd == "/quit":  # stop the app from Telegram (mid-turn: abort first, no waiting)
            if self.busy:
                self.abort()
            self.tg.send(chat_id, "quit: NECRO is shutting down ...")
            self.quit = True
            return
        if cmd == "/status":  # same text as the TUI /status (shared status_text)
            self.tg.send(chat_id, self.status_text())
            return
        if cmd == "/model":  # details / switch, same implementation as the TUI
            self.tg.send(chat_id, self.model_command(arg))
            return
        if cmd == "/tgcommands":  # push ('' / 'update') or clear ('remove') the menu
            self.tg.send(chat_id, self.tg_menu(arg))
            return
        if cmd == "/compact":  # compact the context now; must not race a running turn
            if self.busy:
                self._tg_queued = (text, chat_id, sender)  # runs right after this turn
                self.tg.send(chat_id, "(queued - runs after the current turn)")
                return
            self.compact_command(chat_id)
            return
        if cmd.startswith("/") and cmd not in TG_KNOWN:  # typo: answer instead of spending a turn
            self.tg.send(chat_id, f"unknown command {cmd}\n\n{TG_COMMANDS}")
            return
        if self.busy:
            # same queue behaviour as the TUI - the prompt runs right after
            # this turn instead of being dropped with "(busy - wait)"
            self._tg_queued = (text, chat_id, sender)
            self.tg.send(chat_id, "(queued - runs after the current turn)")
            return
        # acknowledge receipt (short): whitelisted senders get the
        # '(userid in whitelist)' prefix; the final reply is the completion milestone
        ack = "✓" + (f" ({chat_id} in whitelist)" if str(chat_id) in TG_CHATS else "")
        self.tg.send(chat_id, ack)
        self._start_turn(f"[telegram:{sender}] {text}" if sender else f"[telegram] {text}", text, chat_id)
    def tg_run_summary(self):
        """Run summary to the whitelisted chats when the app quits -
        status (turns, context) + the last VERDICT line from log/log.md."""
        if self.tg is None or not self.tg_chats:
            return
        verdict = ""
        try:
            from modules.core.filelock import file_lock  # log.md is written via write_file/edit_file
            with file_lock(Path(__file__).resolve().parent / "log" / "log.md"):
                log = (Path(__file__).resolve().parent / "log" / "log.md").read_text(encoding="utf-8")
            # anchored to '- VERDICT:' at line start: prose that merely mentions
            # 'VERDICT:' (changelog entries) must not count as a verdict line
            for ln in log.splitlines():  # no break: the LAST verdict line is the current one
                if re.match(r"-\s*VERDICT:", ln):
                    verdict = ln.strip().lstrip("- ").strip()
        except OSError:
            pass
        msg = f"run finished: turn {self.turns}, context {self.ctx_used}/{self.ctx} tokens"
        if verdict:
            msg += f"\n{verdict}"
        for cid in self.tg_chats:
            self.tg.send(cid, msg)
    def poll_tg(self):
        """One Telegram poll (event loop): deliver a queued message as a turn.
        Safe when Telegram is disabled (self.tg is None) - the guard is what
        keeps the loop from crashing on app.tg.next()."""
        if self.tg is None:
            return
        if (s := self.tg.stranger()) is not None:  # stranger touched the bot
            chat_id, sender = s
            self.tg.send(chat_id, "you are not permitted in this chat")
            for cid in self.tg_chats:  # alert me (ID + name)
                self.tg.send(cid, f"telegram: stranger {chat_id} ({sender}) touched the bot")
            self.dirty = True
        if (msg := self.tg.next()) is not None:
            text, chat_id, sender = msg
            self.tg_submit(text, chat_id, sender)
            self.dirty = True
    def abort(self):
        self.stop.set()
        for c in list(self.conns):  # copy: stream threads append/remove concurrently
            try:
                if hasattr(c, 'sock') and c.sock is not None:
                    c.sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            try:
                c.close()
            except Exception:
                pass
        self.conns.clear()  # clear, not reassign: a concurrent done() may still hold the list (F5)
def load_emblem():
    """Read the emblem (project root /emblem) - shown once at the top of the TUI log."""
    p = Path(__file__).resolve().parent / "emblem"
    try:
        return p.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return None  # no emblem file: start silently
def startup_info(app):
    """main info blocks (emblem + status lines) - shown at startup AND on /clear."""
    emblem = load_emblem()
    if emblem:
        app.add("pre", emblem)  # once at the top of the log, below the header
    if NECRO_MD_PATH:
        app.add("info", f"NECRO.md loaded: {NECRO_MD_PATH} ({len(NECRO_MD)} chars)")
    elif AGENTS_MD_PATH:
        app.add("info", f"AGENTS.md loaded: {AGENTS_MD_PATH} ({len(AGENTS_MD)} chars)")
    if CFG_PATH:
        app.add("info", f"necro.cfg loaded: {CFG_PATH}")
    if SWARM:
        app.add("info", "swarm: activated", color=ACCENT)
    if app.peer is not None:
        from modules.io.api import PEER_DIR  # real discovery dir (NECRO_PEER_DIR, default /tmp/necro)
        if app.peer.bind_error:
            app.add("error", f"Peer-API bind failed ({app.peer.bind_error}) - fallback to {app.peer.host}:{app.peer.port}")
        # neutral at startup; peer_status_loop turns it green while a peer is online.
        # Stored on app: /clear//new replaces the block, the loop reads it fresh.
        app.peer_blk = app.add("info", f"Peer-API: {app.peer.host}:{app.peer.port} (id {app.peer.id}, {PEER_DIR / ('necro-' + app.peer.id + '.json')})", color=MUTED)
        # start the status loop only ONCE - startup_info runs again on every
        # /clear//new and would otherwise spawn a new endless 30 s thread each time
        if not getattr(app, "_peer_status_started", False):
            app._peer_status_started = True
            threading.Thread(target=peer_status_loop, args=(app,), daemon=True).start()
    if app.tg is not None:
        app.add("info", "Telegram: active (long polling, api.telegram.org)", color=TELEGRAM)  # necro_telegram
def poll_timeout(app):
    """select slice of the main loop. A busy turn must see keys within 50 ms so an
    abort reacts at once (the turn itself runs in a thread); idle can wait 250 ms.
    A pending lone ESC must not wait out the full slice at all."""
    if app.rx == "\x1b":
        return 0.01
    return 0.05 if app.busy else 0.25
def colors_help():
    """Print the color roles (role, index, purpose) - the help for cfg 'colors'."""
    print(f"cfg 'colors' = role -> 256-color index (0-255). Defaults:\n{_palette.table()}")
def main():
    # --telegram enables the Telegram client (default: off); token from NECRO_TELEGRAM_TOKEN or cfg 'telegram_token'
    args = sys.argv[1:]
    if "--colors" in args:  # help only: no terminal, no config, no threads needed
        colors_help()
        return
    tg_enabled = "--telegram" in args or os.environ.get("NECRO_TELEGRAM") == "1"
    if not sys.stdin.isatty():
        sys.exit("necro: stdin is not a terminal")
    app, fd = App(), sys.stdin.fileno()
    start_background(app)  # daemon threads (model probe, swarm availability)
    if REMOTE:  # peer-API + sibling discovery only in remote mode
        from modules.io.api import PeerAPI  # peer discovery + HTTP API
        # model/url are published in the discovery file (a parent sees which model a sibling runs)
        app.peer = PeerAPI(app, host=PEER_HOST, port=PEER_PORT, peer_turn_timeout=PEER_TURN_TIMEOUT_S,
                           model=app.model, url=BASE_URL)
        peer_refresh()
    if tg_enabled:
        token = os.environ.get("NECRO_TELEGRAM_TOKEN") or CFG.get("telegram_token")
        if not token:
            app.add("error", "Telegram: no token (NECRO_TELEGRAM_TOKEN or cfg 'telegram_token')")
        else:
            from modules.io.tg import TelegramBot  # lazy: only when --telegram / NECRO_TELEGRAM=1
            app.tg = TelegramBot(token, fmt=str(CFG.get("telegram_format", "markdown")).lower(),
                                 rich=str(CFG.get("telegram_rich", "auto")).lower(),
                                 whitelist=TG_CHATS)  # stranger routing + token guard
            # register the command menu with Telegram (client '/' menu); best effort
            if app.tg.set_my_commands(TG_COMMAND_SPECS):
                app.add("info", "Telegram: commands registered (setMyCommands)")
            else:
                app.add("warn", "Telegram: setMyCommands failed (menu not updated)")
            app.tg_chats = TG_CHATS  # run summary + stranger alerts
            # once on (re)start, greet the whitelisted chats so the user knows the bot is up
            def _tg_greet():
                emblem = load_emblem()  # emblem as greeting (preformatted); fallback text
                for _cid in TG_CHATS:
                    if emblem:
                        app.tg.send_pre(_cid, emblem)
                    else:
                        app.tg.send(_cid, "Telegram: active")
            threading.Thread(target=_tg_greet, daemon=True).start()
    startup_info(app)  # main info (emblem + status lines); /clear shows the same
    old, dec = termios.tcgetattr(fd), codecs.getincrementaldecoder("utf-8")("replace")
    signal.signal(signal.SIGWINCH, lambda *_: setattr(app, "dirty", True))
    try:
        tty.setraw(fd)
        # alt screen + bracketed paste + underline cursor (DECSCUSR 2) instead of
        # the terminal default block; 1000+1006 reports the wheel as SGR mouse
        # events (otherwise alt-screen "alternate scroll" turns it into arrows,
        # which walk the prompt history instead of the content)
        os.write(1, b"\x1b[?1049h\x1b[?2004h\x1b[?1000h\x1b[?1006h" + CURSOR_UNDERLINE)
        while not app.quit:
            if select.select([fd], [], [], poll_timeout(app))[0]:
                data = os.read(fd, 4096)
                if not data:
                    break
                app.feed(dec.decode(data))
                app.dirty = True
            elif app.rx == "\x1b" and time.time() - app.rx_time > (0.02 if app.busy else 0.06):
                # lone Escape with no following CSI bytes: abort (shorter wait while busy)
                app.rx = ""
                app.key("\x1b")
            elif app.busy:
                # no input and no content change (e.g. a long tool call): animate the
                # spinner via the header-only redraw (no-op when the frame did not change)
                app.draw(spin_only=True)
            if app._cwd_flash and time.time() - app._cwd_flash >= 3.0:  # cwd flash expired: one final full draw
                app._cwd_flash, app.dirty = 0.0, True
            app.poll_tg()
            if app.dirty:
                # content changed: full incremental draw (the spinner thread owns the
                # busy animation)
                app.draw()
                app.dirty = False
    finally:
        app.tg_run_summary()  # run summary (status + VERDICT) to telegram
        if app.peer is not None:
            app.peer.close()
        app.sessions.close()  # checkpoint the WAL, release the sqlite handle
        try:
            os.write(1, b"\x1b[?2004l" + b"\x1b[?1006l\x1b[?1000l" + b"\x1b[?1049l" + CURSOR_DEFAULT + b"\x1b[?25h")
        except OSError:
            pass  # dead terminal (EPIPE): nothing to restore anyway
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
if __name__ == "__main__":
    main()
