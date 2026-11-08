"""Agent loop (inference + tool execution), extracted from necro.py.

Pure logic: no TUI. The App is passed in and used for shared state
(messages, model, ctx, stop, conns) and UI hooks (add/append/set_head).
Config values (base url, timeouts, tool lists, swarm) are injected at
construction so this module has no import dependency on necro.py."""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from modules.core import inference
from modules.io import tools
from modules.io.swarm import find as swarm_find, workers as swarm_workers
from modules.ui.subagent import progress_head, subagent_prompt

CONSULT_TRIM = 2000  # max chars per message in the consult remote context
RETRY_ATTEMPTS = 8  # fast start - the inference server may still load the model
RETRY_MAX_WAIT = 10  # cap (s) for the exponential backoff between attempts


class Agent:
    def __init__(self, app, base, step_timeout, tools_list, sub_tools, swarm, swarm_up,
                 agents_md, agents_md_path, auth=None):
        self.app = app  # App: shared state (messages/model/ctx/stop/conns) + UI hooks
        self.base = base
        self.step_timeout = step_timeout
        self.auth = auth  # optional cfg 'auth' {server: token}
        self.tools = tools_list  # TOOLS (main loop)
        self.sub_tools = sub_tools  # SUB_TOOLS (sub-agents: no delegate/consult/peer)
        self.swarm = swarm  # list of {url, model}
        self.swarm_up = swarm_up  # live availability dict (mutated by the background check)
        self.agents_md = agents_md
        self.agents_md_path = agents_md_path
        self._deleg_fallbacks = 0  # per-turn counters (reset via reset_counters())
        self._deleg_steps = 0

    def reset_counters(self):
        """Reset the per-turn delegation counters (called by App.turn before each turn)."""
        self._deleg_fallbacks, self._deleg_steps = 0, 0

    # ---- inference
    def request(self, body, base=None, model=None):
        # HTTP layer in modules/core/inference.py; here only the global values and
        # the abort tracking (app.conns)
        app = self.app
        body["thinking"] = app.reasoning
        body["model"] = model or app.model
        b = base or self.base
        token = inference.token_for(b, self.auth)  # optional per-server access token
        return inference.open_conn(b, body, app.conns, timeout=self.step_timeout, token=token)

    def done(self, conn):
        """Close a finished connection and drop it from the abort list."""
        try:
            conn.close()
        except Exception:
            pass
        try:
            self.app.conns.remove(conn)
        except ValueError:
            pass

    def _retry(self, fn, what="API"):
        """exponential backoff (capped at RETRY_MAX_WAIT) on connection errors before
        the first byte (server still loading, network hiccup); up to RETRY_ATTEMPTS
        attempts (a fast start can outlive the old 3 s window). A user abort
        (stop set) re-raises immediately without retrying. The backoff sleep is
        interrupted by abort in 0.1 s slices."""
        app = self.app
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return fn()
            except Exception as e:
                if app.stop.is_set() or attempt == RETRY_ATTEMPTS - 1:
                    raise
                wait = min(2 ** attempt, RETRY_MAX_WAIT)
                app.add("info", f"{what}: {type(e).__name__} - Retry in {wait} s (Versuch {attempt + 1}/{RETRY_ATTEMPTS}) …")
                t0 = time.time()
                while time.time() - t0 < wait:
                    if app.stop.is_set():
                        raise RuntimeError("aborted")
                    time.sleep(0.1)

    def complete(self, msgs, base=None, model=None):
        def once():
            conn, r = self.request({"messages": msgs, "stream": False}, base, model)
            try:
                return json.loads(r.read())["choices"][0]["message"].get("content") or ""
            finally:
                self.done(conn)
        return self._retry(once, "complete")

    def complete_tools(self, msgs, base, model):
        """Non-streaming completion with tools (for swarm sub-agents); (content, calls)."""
        def once():
            conn, r = self.request({"messages": msgs, "tools": self.sub_tools, "stream": False}, base, model)
            try:
                return json.loads(r.read())["choices"][0]["message"]
            finally:
                self.done(conn)
        m = self._retry(once, "complete_tools")
        calls = []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append({"id": tc.get("id") or "", "name": fn.get("name") or "", "args": fn.get("arguments") or ""})
        for n, c in enumerate(calls):
            c["id"] = c["id"] or f"call_{n}"  # deterministic fallback: stable across retries (F6)
        return m.get("content") or "", calls

    def _agent_loop(self, sysp, task, base, model, log, blk=None):
        """Sub-agent tool loop (max 10 steps) on (base, model); returns the final text.
        while it runs the tool block header shows live progress (step + elapsed) -
        a delegate can take minutes, the user must see the session is working."""
        t0 = time.monotonic()
        msgs = [{"role": "system", "content": sysp}, {"role": "user", "content": task}]
        for step in range(1, 11):
            self._deleg_steps += 1  # sub-agent work volume
            if blk is not None:  # live progress in the block header
                self.app.set_head(blk, progress_head(model, step, 10, time.monotonic() - t0))
            if self.app.stop.is_set():
                raise RuntimeError("aborted")
            text, calls = self.complete_tools(msgs, base, model)
            m = {"role": "assistant", "content": text or None}
            if calls:
                m["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": c["name"], "arguments": c["args"] or "{}"}} for c in calls]
            msgs.append(m)
            if text:
                log(text)
            if not calls:
                return text or "(no output)"
            for c in calls:
                try:
                    a = json.loads(c["args"] or "{}")
                    brief = a.get("command") or a.get("path") or ""
                except ValueError:
                    brief = "?"
                log(f"  {c['name']}  {brief}")
                res = self.run_tool(c)
                log(f"  \u2192 {res[:400]}")
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": res})
        return "(max 10 steps reached)"

    def delegate(self, ag, task, blk=None):
        """Run a sub-agent loop on a swarm agent (own context, same tools, max 10 steps).
        if the swarm agent fails mid-run (network, server crash), the main agent
        takes over the task itself instead of returning an error."""
        def log(t):
            if blk is not None:
                self.app.append(blk, "\n" + t)
        # skills hint + project context in one pure prompt builder (modules/ui/subagent.py)
        sysp = subagent_prompt(ag["model"], os.getcwd(), self.agents_md, self.agents_md_path)
        try:
            return self._agent_loop(sysp, task, ag["url"], ag["model"], log, blk)
        except Exception as e:
            if self.app.stop.is_set():  # user abort: no fallback, turn ends
                raise
            return self.delegate_fallback(ag, task, e, blk)

    def delegate_fallback(self, ag, task, err, blk=None):
        """the swarm agent died mid-run - the main agent takes over the task itself
        (same loop, local model). An explicit user abort (stop set) is not a swarm failure."""
        if self.app.stop.is_set():
            raise RuntimeError("aborted")
        def log(t):
            if blk is not None:
                self.app.append(blk, "\n" + t)
        self._deleg_fallbacks += 1  # quality signal (swarm agent died mid-run)
        log(f"swarm agent {ag['model']} failed ({type(err).__name__}: {err}) - taken over by main agent")
        sysp = subagent_prompt(self.app.model, os.getcwd(), self.agents_md, self.agents_md_path)
        return self._agent_loop(sysp, task, self.base, self.app.model, log, blk)

    def consult(self, ag, question, blk=None):
        """Bot-to-bot consultation: the remote model may ask follow-up questions (up to 3),
        which the local model answers with its full context. Ends when the remote model is satisfied."""
        def log(t):
            if blk is not None:
                self.app.append(blk, "\n" + t)
        remote = [{"role": "system", "content":
                   f"You are {ag['model']}, consulted by another agent (NECRO). You may ask follow-up questions "
                   "when you need more detail - prefix them with 'QUESTION:' (one at a time, max 3). When you have "
                   "enough, give your final answer prefixed with 'ANSWER:'. Be concise and concrete."}]
        local = [{"role": "system", "content":
                  "You are NECRO answering follow-up questions from a remote agent you are consulting. "
                  "Answer precisely based on your conversation context. No tools needed."}]
        local += [dict(m, content=m["content"][:CONSULT_TRIM] if m["role"] == "tool" and len(m.get("content") or "") > CONSULT_TRIM
                       else m["content"]) for m in self.app.messages[1:]]  # trim old tool results (context bloat)
        # consult runs INSIDE run_calls - the last message is the current assistant
        # message with tool_calls whose tool results are not in the context yet. Strict
        # servers reject 'assistant tool_calls not followed by tool messages' (and
        # complete() sends no tools field): drop the dangling pair head
        if local and local[-1].get("tool_calls"):
            local = local[:-1]
        remote.append({"role": "user", "content": question})
        for round_ in range(4):  # initial answer + up to 3 question rounds
            if self.app.stop.is_set():
                raise RuntimeError("aborted")
            if blk is not None:  # live progress in the block header
                self.app.set_head(blk, progress_head(ag["model"], round_ + 1, 4, 0, kind="consult"))
            text = self.complete(remote, ag["url"], ag["model"])
            log(f"[{ag['model']}] {text}")
            qm = re.search(r"QUESTION:\s*(.+)", text)
            if not qm or round_ == 3:
                return text
            ans = self.complete(local + [{"role": "user", "content": f"Remote agent asks: {qm[1].strip()}\nAnswer briefly."}])
            log(f"[NECRO] {ans}")
            # trim at append time so the remote context stays bounded across rounds
            remote.append({"role": "assistant", "content": text[:CONSULT_TRIM]})
            remote.append({"role": "user", "content": ans[:CONSULT_TRIM]})
        return text

    def compact(self):
        """Keep the context under 80 % of the window (real usage, else estimate).
        cheap first pass - trim oversized tool results (no LLM call); if that
        alone frees enough context the expensive head-summary is skipped entirely
        (a single huge recent tool result would otherwise re-trigger compact() every
        step). Only when trimming is not enough do we summarise the old head, never
        splitting a tool-call pair."""
        from modules.core.context import trim_tool_results, under_after_trim
        app = self.app
        m = app.messages
        est_before = len(json.dumps(m, ensure_ascii=False)) // 3
        used = app.ctx_used  # real prompt_tokens (None before the first API call)
        if (used or est_before) < app.ctx * 0.8:
            return
        m, n = trim_tool_results(m)  # shrink oversized tool results in place (no LLM call)
        if n:
            app.messages = m
            # decide on the REAL usage (scaled by the freed chars), not on the
            # rough char//3 estimate alone - it can sit far below the real usage and
            # skip the head-summary while the context is still over the threshold
            if under_after_trim(used, est_before,
                                len(json.dumps(m, ensure_ascii=False)) // 3, app.ctx):
                app.add("info", f"compacting context ({n} tool results trimmed) …")
                return
        cut = max(1, len(m) - 6)
        while cut > 1 and m[cut]["role"] == "tool":  # never split a tool-call pair
            cut -= 1
        if cut <= 1:  # nothing left to summarise (only system + a few recent messages)
            return
        app.add("info", "compacting context …")
        text = json.dumps(m[1:cut], ensure_ascii=False)[-int(app.ctx * 2.5):]
        summary = self.complete([
            {"role": "system", "content": "Summarise this agent conversation compactly: goals, "
             "decisions, files touched, open tasks. Keep exact paths and commands."},
            {"role": "user", "content": text}])
        app.messages = [m[0], {"role": "user", "content": "[Compacted context]\n" + summary}] + m[cut:]

    def stream(self, tools=None):
        # retry only the connection phase (before the first byte) - once the SSE
        # stream is flowing, a mid-stream error would duplicate already-shown content
        app = self.app
        conn, resp = self._retry(lambda: self.request({"model": app.model, "messages": app.messages,
                                                       "tools": tools or self.tools, "stream": True,
                                                       "stream_options": {"include_usage": True}}), "stream")
        cur, content, calls, in_think = None, [], {}, False
        def out(kind, text):
            nonlocal cur
            if cur is None or cur.kind != kind:
                text = text.lstrip("\n")
                if not text:
                    return
                # reuse the last block of the same kind so consecutive streams of the
                # same kind keep one block instead of spawning a new one per step
                with app.lock:
                    last = app.blocks[-1] if app.blocks else None
                cur = last if last is not None and last.kind == kind else app.add(kind)
            app.append(cur, text)
            if kind == "agent":
                content.append(text)
        try:
            for o in inference.sse_events(resp):  # SSE parsing in modules/core/inference.py
                if app.stop.is_set():
                    break
                if (u := o.get("usage")) and u.get("prompt_tokens"):  # final chunk: real context size
                    app.ctx_used = u["prompt_tokens"]
                    app.last_completion_tokens = u.get("completion_tokens") or 0  # turn trace
                for choice in o.get("choices") or []:
                    d = choice.get("delta") or {}
                    out("think", d.get("reasoning_content") or d.get("reasoning") or "")
                    piece = d.get("content") or ""
                    while piece:  # also handle inline <think> tags
                        tag = "</think>" if in_think else "<think>"
                        i = piece.find(tag)
                        out("think" if in_think else "agent", piece if i < 0 else piece[:i])
                        if i < 0:
                            break
                        piece, in_think = piece[i + len(tag):], not in_think
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        fn = tc.get("function") or {}
                        c["id"] = tc.get("id") or c["id"]
                        c["name"] = fn.get("name") or c["name"]
                        c["args"] += fn.get("arguments") or ""
        finally:
            self.done(conn)
        if app.stop.is_set():
            raise RuntimeError("aborted")
        res = [calls[k] for k in sorted(calls)]
        for n, c in enumerate(res):
            c["id"] = c["id"] or f"call_{n}"  # deterministic fallback: stable across retries (F6)
        return "".join(content), res

    # ---- tools (dispatch dict: one small method per tool)
    def _tool_bash(self, a, blk):
        cmd = a.get("command")
        if not cmd:  # guard: the model omitted the argument
            return "error: bash: missing 'command'"
        # live pump + short-slice abort live in modules/io/tools.py
        o, rc, timed_out, aborted = tools.run_bash(
            cmd, self.app.stop,
            on_line=(lambda ln: self.app.append(blk, ln)) if blk is not None else None)
        if blk is not None:  # already streamed live: turn() skips the res append
            blk.live = True
            note = (f"[timeout after {tools.BASH_TIMEOUT} s]" if timed_out else "[aborted]" if aborted else "")
            parts = ([note] if note else []) + [f"[exit {rc}]"]
            sep = "" if blk.text.endswith("\n") else "\n"  # output ends with a newline already
            self.app.append(blk, sep + "\n".join(parts))
        o = tools.cap_text(o)
        suffix = (f"\n[timeout after {tools.BASH_TIMEOUT} s]" if timed_out
                  else "\n[aborted]" if aborted else "")  # the model sees the cut
        self.app._note_cwd()  # bash may have changed the working dir
        return f"{o}{suffix}\n[exit {rc}]"

    def _tool_recall(self, a, blk):
        hits = self.app.sessions.recall(a["query"])
        return "\n".join(hits) or "no matches in past sessions"

    def _tool_grep(self, a, blk):
        return tools.run_grep(a["pattern"], a.get("path") or ".")

    def _tool_delegate(self, a, blk):
        ag, err = swarm_find(a["model"], self.swarm, self.base, self.app.model)
        if err:
            return err
        return self.delegate(ag, a["task"], blk)

    def _tool_consult(self, a, blk):
        ag, err = swarm_find(a["model"], self.swarm, self.base, self.app.model)
        if err:
            return err
        return self.consult(ag, a["question"], blk)

    def _tool_peer(self, a, blk):
        from modules.io.api import peer_instances, peer_prompt, peer_health, OWN_ID
        ps = [x for x in peer_instances() if x["id"] != OWN_ID]  # not myself
        if not ps:
            return "error: no sibling NECRO instance found"
        p = next((x for x in ps if x["id"] == a.get("instance")), None) or ps[0]
        if blk is not None:  # live progress: show the contacted peer id (primary terminal)
            self.app.set_head(blk, f"peer  [{p['id']}] contacting...")
        _, err = peer_health(p)  # reachability check before sending a big task
        if err:
            return f"error: peer {p['id']} not reachable ({err})"
        # no busy check - the peer queues the turn (waits for the running one)
        return peer_prompt(p, a["task"])  # HTTP call lives in modules/io/api.py

    def _tool_read(self, a, blk):
        return tools.read_file(a["path"], a.get("offset"), a.get("limit"))

    def _tool_write(self, a, blk):
        return tools.write_file(a["path"], a["content"])

    def _tool_edit(self, a, blk):
        return tools.edit_file(a["path"], a.get("old"), a.get("new"))

    _TOOL_FNS = {"bash": _tool_bash, "recall": _tool_recall, "grep": _tool_grep,
                 "delegate": _tool_delegate, "consult": _tool_consult, "peer": _tool_peer,
                 "read_file": _tool_read, "write_file": _tool_write, "edit_file": _tool_edit}

    def run_tool(self, c, blk=None):
        try:
            a = json.loads(c["args"] or "{}")
        except ValueError:
            return "error: invalid JSON arguments"
        fn = self._TOOL_FNS.get(c["name"])
        if fn is None:
            return f"error: unknown tool {c['name']}"
        try:
            return fn(self, a, blk)
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

    @staticmethod
    def _brief(c):
        """One-line preview of a tool call for the tool block head."""
        try:
            a = json.loads(c["args"] or "{}")
            return a.get("command") or a.get("path") or ""
        except ValueError:
            return "?"

    def _run_one(self, c, blk=None):
        """Execute one tool call: tool block, live streaming (bash), truncation.
        Returns (call, result, ms) - per-tool duration for the trace breakdown."""
        if blk is None:
            blk = self.app.add("tool", f"{c['name']}  {self._brief(c)}")
        t0 = time.monotonic()
        res = self.run_tool(c, blk)
        ms = int((time.monotonic() - t0) * 1000)
        if not blk.live:  # bash already streamed its output into the block
            self.app.append(blk, "\n" + res)
        if len(res) > tools.CONTENT_CAP:  # keep head + tail (the middle is usually the noise)
            half = tools.CONTENT_CAP // 2
            res = res[:half] + "\n…[truncated]…\n" + res[-half:]
        return c, res, ms

    def run_calls(self, calls):
        """execute the tool calls of one step. Multiple calls run in parallel
        (ThreadPoolExecutor, max 4 workers) instead of sequentially; the results
        come back in call order so the tool messages stay aligned with the
        assistant message. Single calls stay on the calling thread (no pool overhead)."""
        if len(calls) <= 1:
            return [self._run_one(c) for c in calls]
        # blocks are created first (in call order) so the UI shows them in order;
        # only the tool execution itself runs in parallel
        blks = [self.app.add("tool", f"{c['name']}  {self._brief(c)}") for c in calls]
        # more parallelism when (good) swarm models are online
        with ThreadPoolExecutor(max_workers=swarm_workers(self.swarm_up, self.swarm)) as ex:
            return list(ex.map(lambda p: self._run_one(p[0], p[1]), zip(calls, blks)))
