#!/usr/bin/env python3
"""Live caller: dry_run checks -> route() -> harness_call with fallback, behind env DISPATCH_LIVE.
LIVE iff env DISPATCH_LIVE is exactly "1" OR file state/LIVE exists; otherwise OFFLINE: identical to `dispatch --dry-run`, zero calls.
Keeps check_kill (first, and again before each attempt) and redaction (all output passes D.redact). route() has no cap gate: a missing, null, or 0 cap is no ceiling. stdlib only.
Candidates are ranked harnesses whose score is set, starting at would_send_to. A pin is that harness only.
Fall back to the next candidate on failed_retryable, timed_out, failed_hold:usage, or failed_hold:not_installed. When not pinned, NOT_DISPATCHED (no invoke path) also falls back and cannot end the run. Stop on done, PENDING, and every other status. A pinned uncallable harness tries only the pin. Each call gets cwd under the state dir (same DISPATCH_STATE default as live_file), jobs/<job>, so --trust applies to that scratch dir.
A lease is held only for a real async PENDING (expiry is the pending deadline, else now + ASYNC_DEADLINE_MIN minutes) until that deadline or until the receipt file is on disk. Every other result releases the lease before the next harness.
Exit: 0 only for done or a real PENDING (offline WOULD DISPATCH is still 0); 2 HOLD; 3 KILLED; 5 lease refused; 6 not success, including a final NOT_DISPATCHED or other failure (a pinned uncallable harness exits 6); 7 pinned harness refused; 8 packet redaction hits."""
import argparse, json, os, re, sys, time
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import dispatcher as D

# An uncallable harness falls back. NOT_DISPATCHED stays out of D.WALK_STATUSES so a pinned run does not fall back from it.
LIVE_WALK = D.WALK_STATUSES | {"NOT_DISPATCHED"}

FLAG = "DISPATCH_LIVE"

def live_file(env=None):
    """state/LIVE under $DISPATCH_STATE (default <dispatcher>/state). Present = ON; rm = rollback."""
    e = os.environ if env is None else env
    return os.path.join(e.get("DISPATCH_STATE") or D.C.path("state_dir"), "LIVE")

def live_enabled(env=None):
    """ON iff env DISPATCH_LIVE is exactly "1" OR state/LIVE exists. Default OFF. KILL still wins (checked first)."""
    return (os.environ if env is None else env).get(FLAG) == "1" or os.path.exists(live_file(env))

def rollback_off(env=None):
    """Remove state/LIVE if it is present. Returns a one-line message.

    When env DISPATCH_LIVE is exactly "1", the message says that flag must be unset.
    """
    e = os.environ if env is None else env
    path = live_file(env)
    if os.path.lexists(path):
        os.remove(path)
        msg = "removed %s" % path
    else:
        msg = "LIVE not present at %s" % path
    if e.get(FLAG) == "1":
        msg += "; DISPATCH_LIVE=1 must be unset"
    return msg

def check_pin(out, pin):
    """Pure. (ok, reason). Accept only a ranked harness whose score is set and whose breaker input H > 0.
    A harness in ROUTE_EXCLUDED is refused before rank checks (own usage pool, logging only).
    A route.skipped reason wins; otherwise not ranked, unknown score, or breaker open (H=0)."""
    if pin in D.ROUTE_EXCLUDED:
        return False, "excluded from routing (config route=false)"
    route = (out or {}).get("route") or {}
    for item in route.get("skipped") or []:
        if item[0] == pin:
            return False, item[1]
    row = next((r for r in (route.get("ranked") or []) if r.get("harness") == pin), None)
    if row is None:
        return False, "not ranked"
    if row.get("score") is None:
        return False, "unknown score"
    H = row.get("H")
    if isinstance(H, bool) or not isinstance(H, (int, float)) or not H > 0:
        return False, "breaker open (H=0)"
    return True, None

def _mk_read(reader, h):
    """Async read with a kill handle when the reader is the default CLI reader (accepts cancel=)."""
    import inspect
    try: ok = "cancel" in inspect.signature(reader).parameters
    except (TypeError, ValueError): ok = False
    if not ok: return _async_read(reader, h)
    import usage_layer as UL
    c = UL.ReaderCancel()
    return _async_read(lambda _h: reader(_h, cancel=c), h, cancel=c)

class _async_read:
    """a usage read in a thread. get() waits at most READ_WAIT_S, then logs read_failed (never 0, never blocks the call)."""
    READ_WAIT_S = 45.0
    def __init__(self, reader, h, cancel=None):
        import threading
        self.h, self.res, self.t0, self.secs, self.timed_out = h, None, time.time(), None, False
        self.cancel, self.killed = cancel, []
        def go():
            try: self.res = reader(h)
            except Exception as e:  # a reader that throws still lets the result be written, parse_ok false
                self.res = {"harness": h, "parse_ok": False, "state": "read_failed", "error": type(e).__name__, "pools": {}, "primary": None}
            self.secs = round(time.time() - self.t0, 3)
        self.th = threading.Thread(target=go, daemon=True); self.th.start()
    def get(self):
        self.th.join(self.READ_WAIT_S)
        if self.res is None:
            self.secs = round(time.time() - self.t0, 3); self.timed_out = True
            if self.cancel is not None: self.killed = self.cancel.kill()  # the late reader is killed, not left running
            return {"harness": self.h, "parse_ok": False, "state": "read_failed", "error": "timeout (log-only)", "pools": {}, "primary": None}
        return self.res

def run(packet, ledger, runs, capmap, caps, udir=None, env=None, call_fn=None, pin=None, reader_fn=None, session_fn=None, reap_fn=None):
    """Returns (rc, out). call_fn is injectable for tests only (fake harness); defaults are real.
    Usage : reader_fn(h) defaults to usage_layer.read_usage (the CLI's own /usage) when
    call_fn is the real harness_call.call; an injected call_fn with no reader_fn records "not_read" (never a number).
    Each live call reads just before and just after, appends one usage_layer ledger row
    (<DISPATCH_STATE>/usage-ledger.jsonl) and a dispatch.log line; readings are out["usage"]. Offline reads and writes nothing.
    pin is tests/operator only. None leaves routing unchanged. A refused pin returns 7 and sends nothing.
    A pin in ROUTE_EXCLUDED (config route=false) is that refusal: nothing is leased or called. Only that harness is tried.
    A live packet with redaction hits returns 8 and sends nothing.
    Walks scored candidates on failed_retryable, timed_out, failed_hold:usage, and failed_hold:not_installed.
    When not pinned, NOT_DISPATCHED falls back to the next candidate (LIVE_WALK) and cannot end the run.
    call_fn is passed cwd=<state>/jobs/<job> (created). State dir matches live_file: $DISPATCH_STATE or <dispatcher>/state.
    A pin tries only that harness, so a pinned NOT_DISPATCHED or other failure exits 6 after that one attempt.
    A real PENDING keeps the lease until its deadline or until the receipt file is on disk; every other result releases it before the next harness.
    Exit 0 only for done or PENDING. NOT_DISPATCHED is not success.
    grok-build real calls go through one persistent grok session (session_fn injectable); usage reads are
    async and log-only; a reader that throws or times out logs parse_ok false / read_failed and the result is still written.
    Caps are not a route gate (missing, null, or 0 is no ceiling)."""
    D.check_kill()
    ok, out, hits = D.dry_run(packet, ledger, runs, capmap, caps, udir)
    out["mode"] = "live" if live_enabled(env) else "offline"
    if pin is not None:
        pok, why = check_pin(out, pin)
        if not pok:
            out["handoff"]["status"] = f"HOLD (live: pinned harness {pin} refused: {why})"
            return 7, out
        out["handoff"]["balancer_choice"] = out["route"]["ranked"][0]["harness"]
        out["handoff"]["would_send_to"] = pin
        out["handoff"]["pinned"] = True
    if not live_enabled(env):
        return (0 if ok else 2), out                       # offline: nothing sent
    if not ok:
        out["handoff"]["status"] = "HOLD (live: dry-run checks failed, nothing sent)"; return 2, out
    import harness_call as H
    import usage_layer as UL
    real_call = call_fn is None
    call_fn = call_fn or H.call
    if reader_fn is not None:
        reader = reader_fn
    elif real_call:
        reader = UL.read_usage
    else:
        reader = lambda h: {"harness": h, "parse_ok": False, "state": "not_read", "pools": {}, "primary": None}
    job = out["handoff"]["job"]
    with open(packet) as f: text = f.read()
    nhits = D.redact_text(text)[1]
    if nhits > 0:
        out["handoff"]["status"] = f"HOLD (live: packet has {nhits} redaction hits, refused, nothing queued)"
        out["packet_redaction_hits"] = nhits
        return 8, out
    D.check_kill()
    if pin is not None:
        candidates = [pin]
    else:
        start = out["handoff"].get("would_send_to")
        ranked = (out.get("route") or {}).get("ranked") or []
        scored = [row["harness"] for row in ranked if row.get("score") is not None and row.get("harness")]
        candidates = scored[scored.index(start):] if start in scored else scored
    # Pinned: only the pin is tried, including when that harness is NOT_DISPATCHED.
    walk_on = LIVE_WALK if pin is None else D.WALK_STATUSES
    out["attempts"] = []
    out["usage"] = []
    walked, hit_sum = [], 0
    last, last_h, st = None, None, None
    for h in candidates:
        D.check_kill()
        got, msg = D.lease_acquire(job, h)
        out["lease"] = msg
        if not got:
            out["handoff"]["status"] = "HOLD (live: lease refused, nothing sent)"
            out["handoff"]["walked"] = walked
            return 5, out
        keep = False
        try:
            e = os.environ if env is None else env
            state_dir = e.get("DISPATCH_STATE") or D.C.path("state_dir")  # same default as live_file
            cwd = os.path.join(state_dir, "jobs", job)
            os.makedirs(cwd, exist_ok=True)
            sess = (session_fn or UL.grok_router_session)(cwd) if (real_call and h == "grok-build") else None
            # usage reads are LOG-ONLY and never hold the request. Pre runs async beside the call (grok: the
            # session's newest reading, since one pty cannot read and answer at once); post runs async after it.
            if sess is not None:
                pre_t = None
                lr = sess.last_reading if sess.alive() else None
                before0 = lr if lr else {"harness": h, "parse_ok": False, "state": "read_failed", "reason": "no session reading yet (non-blocking)", "pools": {}, "primary": None}
                rs0 = sess.restarts
            else:
                pre_t = _mk_read(reader, h)
            t_start = time.time()
            if sess is not None:
                with open(packet) as f: prompt = f.read()
                H.bump(h)
                try:
                    crc, ctext, restarted = UL.grok_session_call(sess, prompt)
                except Exception as ex:  # flagged, never 0
                    crc, ctext, restarted = 1, "", True
                    ctext = "session error: %s" % type(ex).__name__
                t_end = time.time()
                st0 = H.exit_map(crc if crc != "timeout" else None, crc == "timeout", ctext); H.record_health(h, st0)
                clean_txt = UL.clean_tui_reply(ctext, prompt)          # reply text without TUI noise
                cl, _ = D.redact({"stdout": clean_txt[-4000:]})
                res = {"harness": h, "argv0": "grok (persistent session)", "rc": crc if crc != "timeout" else None, "status": st0,
                       "secs": round(t_end - t_start, 2), "session_restarted": bool(restarted or sess.restarts != rs0),
                       "raw_screen": UL.reply_digest(ctext), **cl}
                post_t = _async_read(lambda _h: UL.read_session_usage(sess), h)
            else:
                res = call_fn(h, job=job, packet=packet, cwd=cwd)
                t_end = time.time()
                reap = reap_fn or (UL.reap_cwd if real_call else None)  # reap_fn injectable for tests only
                if reap is not None:  # reap the call's stray workers BEFORE the post read boots its own CLI
                    out.setdefault("reaped", []).append({"harness": h, "pids": reap(cwd, since=t_start - 1), "when": "before_post_read"})
                post_t = _mk_read(reader, h)
            before = before0 if sess is not None else pre_t.get()
            after = post_t.get()
            if sess is not None and post_t.timed_out:
                sess.fence()   # (b) a late grok reader must not type into the shared session during the next job
            if real_call and sess is not None:  # grok: after the session read (session pid protected)
                out.setdefault("reaped", []).append({"harness": h, "pids": UL.reap_cwd(cwd, since=t_start - 1, keep=[sess.pid] if sess.pid else None), "when": "after_post_read"})
            if post_t.killed or (pre_t is not None and pre_t.killed):
                out.setdefault("reader_killed", []).append({"harness": h, "pre": pre_t.killed if pre_t else [], "post": post_t.killed})
            rd = res if isinstance(res, dict) else {}
            extra = rd.get("token_extra")
            if sess is not None:  # (e) grok usage for the verified session id, this request's turns only
                extra = UL.grok_session_usage(after_ts=t_start, cwd=sess.cwd)
                out.setdefault("grok_usage_session", []).append((extra or {}).get("verified_session_id"))
                gr = UL.grok_session_reply((extra or {}).get("verified_session_id"), sess.cwd)
                if gr:  # (f) clean reply from the verified session's own chat history; else the TUI-cleaned screen
                    cl2, _ = D.redact({"stdout": gr[-4000:]}); res.update(cl2); res["reply_source"] = "grok chat_history (verified session)"
                else:
                    res["reply_source"] = "clean_tui_reply(screen)"
                rd = res
            tokens, model = UL.scrape_tokens(h, rd.get("stdout", ""), extra)
            row = UL.make_row(job, "router", h, model, rd.get("rc"), t_start, t_end, tokens, before, after, True, rd.get("stdout", ""))
            row["usage_read_overhead_s"] = {"pre": (pre_t.secs if pre_t else 0.0), "post": post_t.secs, "blocking": False}  # own field; non-blocking
            if rd.get("session_restarted"):  # restarted session -> not-exact, never 0
                row["request_usage"] = dict(row["request_usage"], exact=False, flagged="grok session restarted during request: not exact")
            UL.append_row(row, os.path.join(state_dir, "usage-ledger.jsonl"))
            D.dispatch_log_line(job, h, t_start, t_end, UL.primary_slot(before), UL.primary_slot(after), state_dir=state_dir)
            out["usage"].append({"harness": h, "before": before, "after": after, "t_start": t_start, "t_end": t_end,
                                 "request_usage": row["request_usage"]})
            clean, n = D.redact(res)  # call() writes health before returning; this redact does not drop that write
            if not isinstance(clean, dict):
                clean = {"value": clean}
            clean.setdefault("harness", h)
            st = clean.get("status")
            out["attempts"].append(clean)
            hit_sum += n
            last, last_h = clean, h
            walked.append(h)
            if st == "PENDING":  # real async submission: hold the lease until the pending deadline
                keep = True
                if isinstance(res, dict) and res.get("deadline") is not None:
                    expiry = res["deadline"]
                else:
                    expiry = time.time() + H.ASYNC_DEADLINE_MIN * 60
                receipt = res.get("receipt") if isinstance(res, dict) else None
                D.lease_extend(job, h, expiry, receipt=receipt)
        finally:
            rel = None
            if not keep:
                rel = D.lease_release(job, h)  # released before the next harness; an exception releases too
            ld = D.lease_dir()
            out.setdefault("lease_evidence", []).append({"harness": h, "job": job, "kept": keep, "release": rel, "lease_dir": ld,  # evidence
                "lease_files_after": sorted(os.listdir(ld)) if os.path.isdir(ld) else [],
                "kill_file": D.KILL, "kill_present": os.path.exists(D.KILL)})
        if st not in walk_on:
            break
    out["call"] = last
    out["redaction_hits"] = hits + hit_sum
    out["handoff"]["status"] = f"ROUTED LIVE -> {last_h} ({st})"
    out["handoff"]["walked"] = walked
    return (0 if st in ("done", "PENDING") else 6), out

def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if "--off" in args:  # before argparse: packet is not required
        print(rollback_off())
        sys.exit(0)
    D.check_kill()
    ap = argparse.ArgumentParser(description="dispatcher live caller (DISPATCH_LIVE=1 to enable; default OFF)")
    ap.add_argument("packet"); ap.add_argument("--ledger", default=D.C.path("go_ledger"))
    ap.add_argument("--runs", default=D.C.path("runs")); ap.add_argument("--usage-dir")
    ap.add_argument("--harness", help="tests and operator use only: pin this harness after full checks; refusal exits 7; unset leaves routing unchanged")
    x = ap.parse_args(argv); D.check_kill()
    capmap = D.load(os.path.join(HERE, "capability-map.json")); caps = D.load(os.path.join(HERE, "caps.json"))
    rc, out = run(x.packet, x.ledger, x.runs, capmap, caps, x.usage_dir, pin=x.harness)
    print(json.dumps(out, indent=1)); sys.exit(rc)

if __name__ == "__main__": main()
