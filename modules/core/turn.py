"""Agent turn execution (TurnMixin for necro.App): one turn = stream -> tool calls ->
results -> repeat, incl. the step/turn guard rails and the turn trace. Runs in its own
thread (App._start_turn) and never touches the terminal, so it stays headless-testable.
The caps and the peer helpers necro.py owns are injected once via configure()."""
import hashlib, json, os, threading, time

def configure(**kw):
    """Bind the config values necro.py owns (step/turn caps, timeouts, peer helpers)."""
    globals().update(kw)

class TurnMixin:
    def turn(self, chat_id=None, done=None):
        # structured turn trace (traces table in necro.db) - one row per turn
        # + measurable tool-call metrics (quality: fails, tokens: chars, speed: ms)
        t0, tools, err = time.monotonic(), [], None
        t_turns = self.turns  # turn counter at entry - the finally must not clobber a newer turn's state
        t_steps = None  # step count at exit (set at the break - the new turn may have reset self.step)
        tool_fails, tool_chars, tools_ms = 0, 0, 0
        tool_items = []  # (name, result, ms) per call - per-tool breakdown in the trace
        self.agent.reset_counters()  # per-turn delegation counters
        p_steps = p_wall = p_sum = 0  # parallel tool steps (wall vs sum of per-call ms)
        from modules.core.context import tool_stats, tool_detail, delegate_stats
        dedup = []  # last executed calls (name, args-digest, result-digest) - stagnation check
        stop_ticker = threading.Event()
        def ticker():
            while not stop_ticker.wait(4):  # typing indicator expires after ~5s
                self.tg.typing(chat_id)
        if chat_id is not None:
            self.tg.typing(chat_id)  # show "typing…" while we work
            threading.Thread(target=ticker, daemon=True).start()
        tg_milestone = 0  # progress milestone every TG_MILESTONE_EVERY steps (telegram turns only)
        self._last_turn_error = None  # abort reason for the peer reply (do_POST reads it)
        try:
            self.turns += 1
            if MAX_TURNS and self.turns > MAX_TURNS:  # per-session turn cap
                msg = f"max turns per session ({MAX_TURNS}) reached - /new or NECRO_MAX_TURNS"
                self.add("info", msg)
                if chat_id is not None:
                    self.tg.send(chat_id, msg)
                return
            for i in range(MAX_STEPS):
                if self.step != i + 1:
                    self.step = i + 1
                    self.dirty = True  # step counter in the header must refresh
                if self.stop.is_set():
                    raise RuntimeError("aborted")
                if self._queued:  # queued prompt runs at the next step boundary
                    # (before the agent decides its next action) instead of waiting
                    # for the whole turn - the user's input gets attention earlier.
                    # The old turn only runs its finally after this (no more stop
                    # checks, no second submit); the new turn starts in a thread.
                    self._queued = False
                    t = self._queued_text
                    self._queued_text = ""
                    self.stop.clear()  # a user abort of THIS turn must not kill the new one
                    t_steps = self.step  # trace this turn's step count (the new turn resets self.step)
                    self.busy = False  # submit() must start a new turn, not re-queue
                    if self._queued_block is not None:  # the prompt runs NOW, not "after the turn"
                        self._queued_block.text = "(Started early at the step boundary)\n" + t
                        self._queued_block = None
                    threading.Thread(target=self.submit, args=(t,), daemon=True).start()
                    # wait until the new turn has actually started (turns incremented)
                    # so the finally guard below can tell the two turns apart.
                    # Only TURN commands increment turns - a queued command
                    # (/help, /clear, ...) never does, so waiting for it would just
                    # burn the full 5 s deadline for nothing: break immediately.
                    if self._is_turn_text(t):
                        deadline = time.monotonic() + 5
                        while self.turns <= t_turns + 1 and time.monotonic() < deadline:
                            time.sleep(0.01)
                    break  # the old turn is done - its finally must not clobber the new turn
                if TURN_TIMEOUT and time.monotonic() - t0 > TURN_TIMEOUT:  # wall-clock safety break
                    raise TimeoutError(f"turn timeout after {TURN_TIMEOUT} s (NECRO_TURN_TIMEOUT)")
                if REMOTE:  # the peer tool only exists in remote mode - don't glob
                    peer_refresh()  # /tmp + liveness-check siblings for a tool that is not registered
                self.agent.compact()
                # peer turn: dynamic toolset - peer tool only while
                # hops < PEER_MAX_HOPS, delegate/consult only when PEER_TURN_SUBAGENTS
                if getattr(self, '_peer_turn', False):
                    text, calls = self.agent.stream(tools=peer_turn_tools(getattr(self, '_peer_hops', 1)))
                else:
                    text, calls = self.agent.stream()
                m = {"role": "assistant", "content": text or None}
                if calls:
                    m["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                        "name": c["name"], "arguments": c["args"] or "{}"}} for c in calls]
                self.messages.append(m)
                self._last_reply = m  # identity for the peer reply lookup (modules/io/api.py)
                if not calls:
                    self.db_sync()
                    if chat_id is not None:  # final reply of a telegram turn goes back to the chat
                        self.tg.send(chat_id, "✅ done")
                        self.tg.send(chat_id, text or "(no output)")
                    elif not text.strip():
                        # a turn that produced only reasoning (no content, no
                        # tool calls) ends silently in the TUI - the user sees the
                        # thinking block, then an idle prompt, and assumes it stalled.
                        # Show a marker so "done with no visible output" is visible.
                        self.add("info", "(no output - turn ended with reasoning only)")
                    return
                tt = time.monotonic()
                results = []
                for c, res, ms in self.agent.run_calls(calls):  # parallel tool execution
                    tools.append(c["name"])
                    results.append(res)
                    tool_items.append((c["name"], res, ms))
                    self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": res})
                    # the same call with the same result 3x in a row = stagnation
                    a = c["args"] or ""
                    if not isinstance(a, str):  # tests / stubs may pass a dict
                        a = json.dumps(a, sort_keys=True, default=str)
                    dig = (c["name"], hashlib.sha1(a.encode()).hexdigest(),
                           hashlib.sha1(res.encode()).hexdigest())
                    dedup.append(dig)
                    if len(dedup) >= 3 and dedup[-3] == dedup[-2] == dedup[-1]:
                        raise RuntimeError(f"Stagnation: '{c['name']}' 3x in a row with the same result - turn aborted")
                tf, tc = tool_stats(results)  # once per step, not twice
                tool_fails += tf
                tool_chars += tc
                tools_ms += int((time.monotonic() - tt) * 1000)
                if len(calls) > 1:  # parallel efficiency (sum of call ms vs wall time)
                    p_steps += 1
                    p_wall += int((time.monotonic() - tt) * 1000)
                    p_sum += sum(ms for _, _, ms in tool_items[-len(calls):])
                self.db_sync()
                if chat_id is not None and i % TG_MILESTONE_EVERY == TG_MILESTONE_EVERY - 1:  # progress milestone
                    tg_milestone += 1
                    self.tg.send(chat_id, f"⏳ milestone {tg_milestone}: step {i + 1}/{MAX_STEPS}")
            self.add("info", f"max steps ({MAX_STEPS}) reached")
        except Exception as e:
            if self.stop.is_set():
                # a watchdog timeout sets stop too - tell them apart so the
                # parent (do_POST) can report "turn timeout" instead of "aborted"
                wd = getattr(self, "_watchdog_fired", None)
                err = f"turn timeout (watchdog, {wd} s)" if wd else "aborted"
            else:
                err = f"{type(e).__name__}: {e}"
            self._last_turn_error = err  # abort reason for the peer reply (do_POST)
            self.add("error", err)  # aborted gets the error colour, like an LLM connection failure
        finally:
            try:  # the trace must not break the turn (e.g. disk full)
                self.sessions.trace(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), model=self.model,
                                    session=os.environ.get("NECRO_SESSION", "run"),
                                    steps=t_steps if t_steps is not None else self.step, tools=tools, error=err,
                                    tool_fails=tool_fails, tool_chars=tool_chars, tools_ms=tools_ms,
                                    tool_detail=tool_detail(tool_items),  # per-tool breakdown
                                    delegates=delegate_stats(tool_items, self.agent._deleg_fallbacks,
                                                            self.agent._deleg_steps,
                                                            self.agent._deleg_sub),
                                    parallel={"steps": p_steps, "wall_ms": p_wall, "sum_ms": p_sum},
                                    prompt_tokens=self.ctx_used,
                                    completion_tokens=self.last_completion_tokens,
                                    latency_ms=int((time.monotonic() - t0) * 1000))
            except Exception:
                pass
            stop_ticker.set()
            if done is not None:
                done.set()
            # a queued prompt may have started a NEW turn before this finally
            # runs - its busy/step state must not be clobbered by this one
            if self.turns == t_turns + 1:  # no newer turn started while this one was winding down
                self.busy, self.step = False, 0  # step reset (state leak: it would survive an aborted turn)
            self.dirty, self.tab_hint, self.menu = True, "", None  # queued prompt: hint goes away
            if self._queued:  # queued prompt: runs right after this turn
                self._queued = False
                t = self._queued_text
                self._queued_text = ""
                threading.Thread(target=self.submit, args=(t,), daemon=True).start()
            if self._tg_queued is not None:  # queued telegram prompt: same
                text, chat_id, sender = self._tg_queued
                self._tg_queued = None
                threading.Thread(target=self.tg_submit, args=(text, chat_id, sender), daemon=True).start()
