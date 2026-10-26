#!/usr/bin/env python3
"""Harness call: runs one harness CLI. Standard library only.
Called by the router (live.py) to run one harness CLI; it never calls any other agent or service.
Every call: KILL first -> subprocess with hard timeout (default 60 s) -> exit map -> redaction.
Caps: per-harness test_call_cap from caps.json never refuses a call; missing, null, 0, or a positive cap is no ceiling. Cursor via cursor-agent absolute path."""
import hashlib, json, os, subprocess, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatcher as D
import drconfig as C

def _cap(v):
    """caps.json test_call_cap: missing, null, 0 or unparsable = no ceiling (None). Only a positive int ceilings."""
    try: v = int(v) if v is not None else None
    except (TypeError, ValueError): return None
    return v if v and v > 0 else None
def load_caps(path=None):
    try: data = D.load(path or os.path.join(D.HERE, "caps.json"))
    except Exception: data = {}
    return {h: _cap((data.get(h) or {}).get("test_call_cap")) for h in C.harnesses()}
CAPS = load_caps()
import drconfig as C
ASYNC = {h for h, v in C.harnesses().items() if v.get("async")}  # CA 1: async PENDING -> receipt file, no sync 60 s timeout
ASYNC_DEADLINE_MIN = 30
NOT_DISPATCHED = "NOT_DISPATCHED"  # no invoke path: not a submission, writes no health record
TIMEOUT = 60
HARNESS_TIMEOUT = {h: v["timeout_s"] for h, v in C.harnesses().items() if v.get("timeout_s")}  # config timeout_s
# documented invoke contracts (argv templates) come from densityrouter.config harnesses.<h>.invoke.
# PROMPT is the packet-text slot, filled by build_argv.
PROMPT = "{prompt}"
def _pinned_bin(env_name, pinned, allow, env=None):
    """Accept an env override only when it is an absolute path of an allowlisted binary.

    os.path.realpath(value) must equal os.path.realpath of some path in allow.
    A relative path, a non-allowlisted absolute path, or an unreadable path
    falls back to pinned. Cursor argv and timeouts are not decided here.
    """
    e = os.environ if env is None else env
    val = e.get(env_name) if hasattr(e, "get") else None
    if isinstance(val, str) and os.path.isabs(val):
        try:
            real = os.path.realpath(val)
        except OSError:
            return pinned
        for cand in allow:
            try:
                if real == os.path.realpath(cand):
                    return val
            except OSError:
                continue
    return pinned

# Binaries: harnesses.<h>.bin, overridable only via harnesses.<h>.bin_env with an allowlisted absolute path.
# Never --force/--yolo/-f/--mode auto/--dangerously-skip-permissions (see forbidden()).
def _bin_for(h):
    v = C.harness(h)
    if v.get("bin") and C.is_remote(h): return v["bin"]  # path on the run_host; "~" is expanded there
    pinned = C.expand(v.get("bin"))
    if not pinned: return None
    if not v.get("bin_env"): return pinned
    allow = {pinned} | {C.expand(p) for p in (v.get("allow_bins") or []) if p}
    return _pinned_bin(v["bin_env"], pinned, allow)
BINS = {h: _bin_for(h) for h in C.harnesses()}
def _template(h):
    t = C.harness(h).get("invoke")
    return None if t is None or not BINS.get(h) else [BINS[h] if a == "{bin}" else a for a in t]
INVOKE = {h: _template(h) for h in C.harnesses()}
GROK_BUILD_BIN = BINS.get("grok-build"); _GROK_PINNED = C.expand(C.harness("grok-build").get("bin"))
CURSOR_BIN = BINS.get("cursor"); _CURSOR_PINNED = C.expand(C.harness("cursor").get("bin"))
GROK_AGENT_MARK = "grok-"   # ~/.grok/bin/agent -> ~/.grok/downloads/grok-<ver>-linux-x86_64
def agent_guard(which=None):
    """Report-only: `agent` on PATH must resolve to the Grok Build binary. Never fixes, never blocks."""
    import shutil
    w = (which or shutil.which)("agent")
    if not w: return {"ok": False, "issue": "agent not on PATH (Grok Build missing?)"}
    real = os.path.realpath(w)
    ok = "/.grok/" in real and os.path.basename(real).startswith(GROK_AGENT_MARK)
    return {"ok": ok, "agent": w, "resolves": real, **({} if ok else {"issue": "agent does not resolve to Grok Build"})}
def build_argv(harness, packet_text):
    """Pure: copy the invoke template and put packet_text in every prompt slot. None if no template."""
    tmpl = INVOKE.get(harness)
    if tmpl is None: return None
    return [packet_text if a == PROMPT else a for a in tmpl]
FORBIDDEN_ARGS = {"--dangerously-skip-permissions", "--mode", "--force", "--yolo", "-f"}
import re, shlex
def forbidden(argv):
    """Parsed/prefix matching. Catches --mode=auto, '--mode auto', --dangerously-skip-permissions=true,
    unambiguous abbreviations (--mod, --dangerously), and flags hidden inside one combined argv string.
    -f is an exact token. --force and --yolo use the same prefix and =value rule. A -- separator stops
    scanning: later argv tokens are packet text and are not flags."""
    toks = []
    for a in argv:
        try: toks += shlex.split(str(a))
        except ValueError: toks += str(a).split()
    for t in toks:
        if t == "--": break
        if t == "-f": return "-f"
        if not t.startswith("--"): continue
        name = t.split("=", 1)[0].lower()
        for f in FORBIDDEN_ARGS:
            if not f.startswith("--"): continue
            if name == f or (len(name) >= 5 and f.startswith(name)): return f
    return None
QUOTA_HINTS = ("quota", "rate limit", "429", "resource_exhausted", "usage limit", "credits")

def counter_path(): return os.path.join((os.environ.get("DISPATCH_STATE") or C.path("state_dir")), "calls.json")

def used(h):
    p = counter_path()
    if not os.path.exists(p): return 0
    with open(p) as f: return json.load(f).get(h, 0)

def bump(h):
    p = counter_path(); os.makedirs(os.path.dirname(p), exist_ok=True)
    d = {}
    if os.path.exists(p):
        with open(p) as f: d = json.load(f)
    d[h] = d.get(h, 0) + 1
    with open(p, "w") as f: json.dump(d, f)

def exit_map(rc, timed_out, out):
    if timed_out: return "timed_out"
    low = out.lower()
    if any(q in low for q in QUOTA_HINTS): return "failed_hold:usage"
    if rc == 0: return "done"
    if rc in (124, 137, -9, -15): return "cancelled"
    if rc in (126, 127): return "failed_hold:not_installed"
    return "failed_retryable"

FAIL_CLASSES = D.FAIL_CLASSES

def read_health(path=None):
    """{} if the health file is missing or unreadable."""
    return D.read_health(path)

def record_health(harness, status, now=None):
    """Write {class, ts} for done and failure classes. Anything else leaves the file as it is.
    Atomic tmp + os.replace. Does not run a subprocess."""
    if status != "done" and status not in FAIL_CLASSES:
        return None
    now = time.time() if now is None else float(now)
    path = D.health_path()
    data = D.read_health(path)
    data[harness] = {"class": status, "ts": now}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return data[harness]

def _record_stale_once(harness, deadline):
    """Record STALE at the deadline, the moment the job became stale.

    Leave a health row whose ts is already >= deadline, so a later done or failure
    is not replaced and a repeat poll does not move the failure time.
    """
    prev = read_health().get(harness)
    if isinstance(prev, dict):
        try:
            if float(prev["ts"]) >= float(deadline):
                return prev
        except (TypeError, ValueError, KeyError):
            pass
    return record_health(harness, "STALE", now=deadline)

def pending_path(job): return os.path.join((os.environ.get("DISPATCH_STATE") or C.path("state_dir")), "pending", f"{job}.json")

def submit_async(harness, job, receipt=None, deadline_min=ASYNC_DEADLINE_MIN, now=None, packet=None):
    """CA 1: record PENDING; completion = receipt file appears. Nothing is sent from here (the caller
    polls for the receipt file). Idempotent on job. packet, when given, stores the path, sha256, and exact text.
    A packet whose decoded text has redaction hits is REFUSED and nothing is written."""
    now = now or time.time(); p = pending_path(job)
    raw = text = None
    if packet is not None:
        with open(packet, "rb") as f: raw = f.read()
        text = raw.decode("utf-8", errors="surrogateescape")
        hits = D.redact_text(text)[1]
        if hits > 0:
            return {"harness": harness, "job": job, "status": "REFUSED",
                    "reason": "packet has redaction hits", "redaction_hits": hits}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p): return poll_async(job, now)
    rec = {"harness": harness, "job": job, "status": "PENDING", "receipt": receipt, "submitted": now,
           "deadline_min": deadline_min, "deadline": now + deadline_min * 60, "stale": False}
    if packet is not None:  # exact file bytes: no strip, no redaction, nothing from the environment
        rec["packet"] = packet
        rec["packet_sha256"] = hashlib.sha256(raw).hexdigest()
        rec["packet_text"] = text
    with open(p, "w") as f: json.dump(rec, f)
    return rec

def poll_async(job, now=None):
    now = now or time.time(); p = pending_path(job)
    if not os.path.exists(p): return {"job": job, "status": "UNKNOWN"}
    with open(p) as f: rec = json.load(f)
    if rec.get("receipt") and os.path.exists(rec["receipt"]):
        with open(rec["receipt"], errors="replace") as f: body = f.read()[-2000:]
        clean, hits = D.redact({"excerpt": body})  # L2: async receipt output redacted like sync stdout
        rec.update(status="done", stale=False, receipt_sha=D.sha256f(rec["receipt"]), excerpt=clean["excerpt"], redaction_hits=hits)
        if rec.get("harness"):
            record_health(rec["harness"], "done", now=now)
    else:
        rec["stale"] = now > rec["deadline"]; rec["status"] = "STALE" if rec["stale"] else "PENDING"
        if rec["status"] == "STALE" and rec.get("harness"):
            _record_stale_once(rec["harness"], rec["deadline"])  # do not rewrite the pending file
    return rec

def cap_label(h): c = CAPS.get(h); return f"{used(h)}/{c if c else 'none'}"

def call(harness, argv=None, timeout=None, cwd=None, job=None, receipt=None, packet=None):
    if timeout is None: timeout = HARNESS_TIMEOUT.get(harness, TIMEOUT)
    D.check_kill()
    if harness in ASYNC:
        rec = submit_async(harness, job or "adhoc", receipt=receipt, packet=packet)
        if rec.get("status") == "REFUSED":
            return rec  # redaction refusal is not a queued success (not done / PENDING)
        return rec
    if argv is None and INVOKE.get(harness) is not None:
        if packet is None: return {"harness": harness, "status": "REFUSED", "reason": "no packet"}  # no smoke ping
        with open(packet) as f: argv = build_argv(harness, f.read())
    if not argv: return {"harness": harness, "status": NOT_DISPATCHED, "reason": "no invoke path on this host"}
    if forbidden(argv): return {"harness": harness, "status": "REFUSED", "reason": f"forbidden flag {forbidden(argv)}"}
    remote = C.is_remote(harness)  # config run_host: ssh -> the CLI lives on that host; no local checks
    guard = agent_guard() if harness == "cursor" and not remote else None
    if guard and not guard["ok"]: print(f"harness_call WARN cursor agent_guard: {guard['issue']} ({guard.get('resolves')})", file=sys.stderr)
    if harness == "cursor" and not remote and not os.path.exists(argv[0]):
        return {"harness": harness, "status": "failed_hold:not_installed", "reason": f"cursor-agent missing at {argv[0]}", "agent_guard": guard}
    bump(harness); t0 = time.time(); timed_out = False
    try:
        p = subprocess.run(C.wrap(harness, argv), capture_output=True, text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        timed_out, rc = True, None
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = ""
    except FileNotFoundError:
        rc, out, err = 127, "", "not found"
    status = exit_map(rc, timed_out, out + err)
    auth = "not_authed" if harness == "cursor" and rc not in (0, None) and "not logged in" in (out + err).lower() else None
    record_health(harness, status)
    clean, hits = D.redact({"stdout": out[-2000:], "stderr": err[-2000:]})
    return {"harness": harness, "argv0": os.path.basename(argv[0]), "rc": rc, "status": status,
            "secs": round(time.time() - t0, 2), "calls_used": used(harness), "redaction_hits": hits, **({"agent_guard": guard} if guard else {}), **({"reason": "cursor-agent not logged in"} if auth else {}), **clean}

if __name__ == "__main__":
    print(json.dumps(call(sys.argv[1]), indent=1))
