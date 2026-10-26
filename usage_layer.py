#!/usr/bin/env python3
"""Router usage layer. Stdlib only.

PRIMARY: each CLI's own usage readout is read before and at the end of every request.
  grok-build  interactive `grok` -> `/usage` (Usage limit tab): "Weekly limit (<plan>) .. N%" (used) and "Resets: <Mon> <d>, HH:MM" (local).
  cursor      interactive `cursor-agent` -> `/usage`: "Included N% used" (monthly plan), "On-Demand $a / $b". No reset printed.
  agy-gemini  interactive `agy` -> `/usage` (Models & Quota): "<group> Weekly/Five Hour Limit Remaining [bar] N.NN%" and "Refreshes in Hh Mm".
  config log_only_pools: logged, never routed, no CLI read: readings are "unknown" (never 0, never estimated).
  The router reads only the harnesses in densityrouter.config; it reads and calls nothing else.
The interactive commands are driven through a stdlib pty (no installs, no keystrokes beyond the slash command, Esc, then SIGTERM).
FALLBACK per request: tokens the CLI prints (cursor json `usage`; grok `grok usage <session>`); anything not printed is "unknown".
Ledger: one JSON row per request: ts, request_id, bot_id, harness, model, tokens, pre, post, delta, parse_ok, flagged.
Rollups: daily and weekly per pool. Pools are separate (no shared pool): one per config harness plus config log_only_pools.
Alerts at 75% and 90% used only write a log line (usage-alerts.log). No throttle in this build (not yet; "We haven't entered
the throttle phase yet"): THROTTLE_PCT stays None and nothing blocks or slows a request.
exact per-request usage = CLI-printed request tokens; each row also logs pool pre/post; batch_match compares the
cumulative pool delta with summed printed tokens; AGY (no printed tokens) logs the per-request % delta marked coarse. No estimates.
"""
import fcntl, json, os, re, select, signal, struct, subprocess, sys, termios, time, pty, uuid
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UNKNOWN = "unknown"
ALERT_PCTS = (75.0, 90.0)
THROTTLE_PCT = None          # no throttle phase yet; unused. Only the owner sets a number.
import drconfig as C
# separate pools; routed=False means logging only (log_only_pools). All from densityrouter.config.
POOLS = {**{p: {"routed": False, "reader": None} for p in (C.load().get("log_only_pools") or {})},
         **{h: {"routed": True, "reader": h} for h in C.routed()}}
HARNESS_POOL = {**{h: v["pool"] for h, v in C.harnesses().items() if v.get("pool")},
                **{p: (v or {}).get("pool", p) for p, v in (C.load().get("log_only_pools") or {}).items()}}

def _bins():
    import harness_call as HC  # pinned, allowlisted binaries (densityrouter.config harnesses.<h>.bin)
    return {h: HC.BINS[h] for h in C.routed() if HC.BINS.get(h)}

# ---------- pty driver ----------
ANSI = re.compile(rb"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1bP[^\x1b]*\x1b\\|\x1b[()][A-Z0-9]|\x1b[=>78DEHMc]")
REPLIES = [(b"\x1b[6n", b"\x1b[24;1R"), (b"\x1b[c", b"\x1b[?62;22c"), (b"\x1b[0c", b"\x1b[?62;22c"),
           (b"\x1b[>0q", b"\x1bP>|xterm(380)\x1b\\"), (b"\x1b[>c", b"\x1b[>41;380;0c"), (b"\x1b[?u", b"\x1b[?0u"),
           (b"\x1b]11;?", b"\x1b]11;rgb:0000/0000/0000\x1b\\"), (b"\x1b]10;?", b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\")]

TRUST_RE = re.compile(r"(?i)trust (this|the) (folder|directory|workspace)|do you trust")

class TrustPromptRefused(RuntimeError):
    pass

def run_group(argv, timeout, cwd=None):
    """subprocess with its own process group; on timeout the whole group is killed (os.killpg). Returns (rc, stdout)."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                         cwd=cwd, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        try: os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        p.communicate()
        return "timeout", ""

def _proc_start(pid):
    """Process start time (epoch s) from /proc, or None."""
    try:
        with open("/proc/%d/stat" % pid) as f: st = f.read().rsplit(")", 1)[1].split()
        with open("/proc/stat") as f: btime = int(next(l for l in f if l.startswith("btime")).split()[1])
        return btime + int(st[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None

def reap_cwd(cwd, since, sig=signal.SIGTERM, proc="/proc", keep=None):
    """(g) Kill our own processes whose cwd is `cwd` and that started at or after `since` (stray CLI workers). Returns pids."""
    if not cwd: return []
    cwd = os.path.realpath(cwd); me = os.getpid(); out = []
    try: pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
    except OSError: return []
    keep = {me, os.getppid()} | set(keep or ())  # never the caller itself or its parent (or pids the caller protects)
    for pid in pids:
        if pid in keep: continue
        try:
            if os.stat("%s/%d" % (proc, pid)).st_uid != os.getuid(): continue
            if os.path.realpath(os.readlink("%s/%d/cwd" % (proc, pid))) != cwd: continue
        except OSError:
            continue
        t = _proc_start(pid)
        if t is None or t < since: continue
        try: os.kill(pid, sig); out.append(pid)
        except ProcessLookupError: pass
    return out

TUI_NOISE = re.compile(r"[\u2800-\u28ff\u2500-\u259f\u00b7\u2026]")  # braille spinner, box drawing, blocks

def clean_tui_reply(text, prompt=None):
    """Fallback plain reply text from a grok TUI screen. Drops everything up to the echoed
    prompt's last line, the 'Worked for' footer, spinner/box/bar glyphs and blank lines. Keeps every number (1-2 digit
    ones included) and the line breaks; only runs of spaces inside a line are collapsed."""
    t = (text or "").replace("\r", "")
    if prompt:
        last = [l.strip() for l in prompt.strip().splitlines() if l.strip()]
        if last:
            k = t.rfind(last[-1])
            if k >= 0: t = t[k + len(last[-1]):]
    t = re.split(r"Worked for \d", t)[0]
    lines = [re.sub(r"[ \t]+", " ", TUI_NOISE.sub(" ", l)).strip() for l in t.split("\n")]
    lines = [re.sub(r"\s+([.,;:)])", r"\1", l) for l in lines if l]
    return "\n".join(lines).strip()

def screen_text(raw):
    raw = re.sub(rb"\x1b\[\d*(;\d*)?[Hf]", b"\n", raw)
    return ANSI.sub(b"", raw).replace(b"\r", b"").decode("utf-8", "replace")

class ReaderCancel:
    """handle a usage reader registers its child in; kill() at the 45 s limit kills it (process group)."""
    def __init__(self):
        import threading
        self.ev = threading.Event(); self.pids = []; self.killed = []
    def is_set(self): return self.ev.is_set()
    def kill(self):
        self.ev.set()
        for pid in list(self.pids):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try: os.killpg(pid, sig); time.sleep(0.2)
                except (ProcessLookupError, PermissionError): break
            self.killed.append(pid)
        return self.killed

class CliExitedEarly(RuntimeError):
    """The usage CLI exited before the capture finished (e.g. agy closed its tty while "Signing in...").
    Raised so the read is logged as read_failed with this name, never as a silent short parse_failed."""

class ReaderCancelled(Exception):
    pass

def pty_capture(argv, keys, cwd=None, boot_s=9.0, after_s=10.0, rows=50, cols=200, cancel=None):
    """Start argv on a pty, type `keys`, Enter, capture the screen, Esc, then terminate. Returns screen text."""
    env = dict(os.environ, TERM="xterm-256color", COLUMNS=str(cols), LINES=str(rows))
    pid, fd = pty.fork()
    if pid == 0:
        if cwd: os.chdir(cwd)
        try: os.execve(argv[0], argv, env)
        finally: os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    if cancel is not None: cancel.pids.append(pid)
    buf = bytearray(); t_boot = time.time() - 1; t0 = time.time(); exited = {}
    def child_gone():
        """EOF/EIO on the pty master: has the CLI really exited, or did it only drop its tty for a moment?"""
        if exited: return True
        try: wp, st = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError: wp, st = pid, 0
        if wp == pid:
            exited["rc"] = os.waitstatus_to_exitcode(st) if hasattr(os, "waitstatus_to_exitcode") else st
            exited["after_s"] = round(time.time() - t0, 3)
            return True
        return False
    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            if cancel is not None and cancel.is_set(): raise ReaderCancelled("usage reader killed at the read limit")
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r: continue
            try: d = os.read(fd, 65536)
            except OSError: d = b""
            if not d:  # EOF/EIO: previously a silent early return (2026-10-25 post read: 5.95 s, parse_failed)
                if cancel is not None and cancel.is_set(): raise ReaderCancelled("usage reader killed at the read limit")
                if child_gone():
                    raise CliExitedEarly("usage CLI exited after %ss (rc %s) before the screen was captured"
                                         % (exited["after_s"], exited["rc"]))
                time.sleep(0.2); continue  # child alive: keep pumping until this step's deadline
            buf.extend(d)
            for q, a in REPLIES:
                if q in d: os.write(fd, a)
    try:
        pump(boot_s)
        if TRUST_RE.search(screen_text(bytes(buf))):  # never answer a folder-trust prompt (CA advice): Esc and fail the read
            os.write(fd, b"\x1b"); pump(0.5)
            raise TrustPromptRefused("CLI asked to trust %s; not answered" % (cwd or os.getcwd()))
        os.write(fd, keys.encode()); pump(2.0)
        if TRUST_RE.search(screen_text(bytes(buf))):  # a late-rendered trust prompt is re-checked before Enter
            os.write(fd, b"\x1b"); pump(0.5)
            raise TrustPromptRefused("CLI asked to trust %s after keys; Enter not sent" % (cwd or os.getcwd()))
        os.write(fd, b"\r"); pump(after_s); os.write(fd, b"\x1b"); pump(1.0)
    finally:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(pid, sig); time.sleep(0.5)   # pty.fork child is a session leader: kill its whole group
            except ProcessLookupError: break
        if not exited:
            try: os.waitpid(pid, 0)
            except ChildProcessError: pass
        os.close(fd)
        if cwd and os.path.realpath(cwd) != os.path.realpath(os.getcwd()):
            reap_cwd(cwd, since=t_boot)  # (g) helpers the CLI detached (e.g. cursor worker-server); only an explicit reader dir
    return screen_text(bytes(buf))

# ---------- parsers (screen text -> pools) ----------
MONTHS = {m: i for i, m in enumerate(["january","february","march","april","may","june","july","august","september","october","november","december"], 1)}

def _local_reset(mon, day, hh, mm, now):
    now = now.astimezone()
    dt = now.replace(month=MONTHS[mon.lower()], day=int(day), hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if dt < now - timedelta(days=1): dt = dt.replace(year=dt.year + 1)
    return dt.isoformat()

def parse_grok(text, now):
    # Anchored: the used % is the first line after "Weekly limit (<plan>)" that is only "N%"; up to 8 digit-free lines between (bar and TUI borders) are skipped.
    m = re.search(r"^\s*Weekly limit\s*\(([^)\n]*)\)[^\n]*\n(?:[^\n%\d]*\n){0,8}?()\s*(\d+(?:\.\d+)?)%\s*$", text, re.M)
    r = re.search(r"Resets:\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{1,2}):(\d{2})", text)
    if not m: return {}
    used = float(m.group(3))
    return {"weekly": {"plan": m.group(1).strip(), "used_pct": used, "remaining_pct": round(100.0 - used, 4),
                       "reset_at": _local_reset(*r.groups(), now) if r else UNKNOWN, "window": "weekly"}}

def _month(name):
    n = name.lower()
    for full, i in MONTHS.items():
        if len(n) >= 3 and full.startswith(n): return i
    return None

def _date_reset(mon, day, now):
    """Date-only reset (time not printed): ISO date, no invented time."""
    m = _month(mon)
    if not m: return UNKNOWN
    d = now.astimezone().date().replace(month=m, day=int(day))
    if d < now.astimezone().date(): d = d.replace(year=d.year + 1)
    return d.isoformat()

def _num(x): return int(x.replace(",", ""))

def parse_grok_session(text):
    """grok /usage 'Usage limit' tab, 'Session usage (since start or last resume): Input tokens: N (C cached)
    Output N (R reasoning) Total N Model calls: N . API time: Xs Cost: $D'. 'no model calls yet' is a printed zero.
    Returns {} when the block is not on screen (unknown, never 0)."""
    t = re.sub(r"\s+", " ", text)
    if re.search(r"Session usage: no model calls yet", t):
        return {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0, "model_calls": 0, "cost_usd": 0.0, "printed": "no model calls yet"}
    m = re.search(r"Session usage \(since start or last resume\): Input tokens: ([\d,]+) \(([\d,]+) cached\) Output ([\d,]+) "
                  r"\(([\d,]+) reasoning\) Total ([\d,]+) Model calls: ([\d,]+).*?Cost: \$([\d.]+)", t)
    if not m: return {}
    return {"input": _num(m.group(1)), "cached": _num(m.group(2)), "output": _num(m.group(3)), "reasoning": _num(m.group(4)),
            "total": _num(m.group(5)), "model_calls": _num(m.group(6)), "cost_usd": float(m.group(7)), "printed": "session usage"}

def session_delta(pre, post):
    """in/out/$ delta between two /usage session readings in the SAME CLI session. Unknown when either side is missing."""
    a, b = (pre or {}).get("session") or {}, (post or {}).get("session") or {}
    if not a or not b: return {"input": UNKNOWN, "output": UNKNOWN, "cost_usd": UNKNOWN}
    return {"input": b["input"] - a["input"], "output": b["output"] - a["output"],
            "cost_usd": round(b["cost_usd"] - a["cost_usd"], 6)}

def parse_cursor(text, now):
    out = {}
    rr = re.search(r"\bResets\s+([A-Z][a-z]{2,8})\.?\s+(\d{1,2})\b", text)
    reset = _date_reset(rr.group(1), rr.group(2), now) if rr else UNKNOWN
    for name, key in (("included", "Included"), ("auto", "Auto"), ("api", "API")):
        m = re.search(r"^\s*" + key + r"\s+(\d+(?:\.\d+)?)% used", text, re.M)
        if m:
            used = float(m.group(1))
            out[name] = {"used_pct": used, "remaining_pct": round(100.0 - used, 4), "reset_at": reset, "window": "monthly"}
    m = re.search(r"On-Demand\s+\$([\d.]+)\s*/\s*\$([\d.]+)", text)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        out["on_demand"] = {"spent_usd": a, "limit_usd": b, "used_pct": round(100.0 * a / b, 4) if b else UNKNOWN,
                            "remaining_pct": round(100.0 - 100.0 * a / b, 4) if b else UNKNOWN, "reset_at": reset, "window": "monthly"}
    return out

def parse_agy(text, now):
    out = {}; group = None; label = None
    for line in text.splitlines():
        s = line.strip()
        if re.fullmatch(r"[A-Z0-9][A-Z0-9 .&-]+", s) and len(s) > 3: group = s.lower().replace(" ", "_"); continue
        m = re.match(r"(.*?)(Weekly|Five Hour) Limit( Remaining)?$", s)
        if m and group:
            label = "%s.%s" % (group, "weekly" if m.group(2) == "Weekly" else "five_hour"); continue
        m = re.search(r"\]\s*(\d+(?:\.\d+)?)%", s)
        if m and label and label not in out:
            rem = float(m.group(1))
            out[label] = {"remaining_pct": rem, "used_pct": round(100.0 - rem, 4), "reset_at": UNKNOWN,
                          "window": label.rsplit(".", 1)[1]}
            continue
        m = re.search(r"Refreshes in\s*(?:(\d+)h)?\s*(?:(\d+)m)?", s)
        if m and label in out and (m.group(1) or m.group(2)):
            h = int(m.group(1) or 0); mi = int(m.group(2) or 0)
            out[label]["reset_at"] = (now.astimezone() + timedelta(hours=h, minutes=mi)).replace(second=0, microsecond=0).isoformat()
            out[label]["resets_in"] = "%dh %dm" % (h, mi)
    return out

_PARSER_FUNCS = {"grok": parse_grok, "cursor": parse_cursor, "agy": parse_agy}  # add a parser here for a new /usage format
def _usage(h): return C.harness(h).get("usage") or {}
_USAGE_H = [h for h in C.harnesses() if _usage(h).get("parser") in _PARSER_FUNCS]
PARSERS = {h: _PARSER_FUNCS[_usage(h)["parser"]] for h in _USAGE_H}
PRIMARY = {h: _usage(h).get("primary") for h in _USAGE_H}
SOURCE = {h: _usage(h).get("source") for h in _USAGE_H}

# /usage readers run in a scratch dir each CLI already trusts (config harnesses.<h>.usage.reader_cwd); no trust or config change
READER_CWD = {h: C.expand(_usage(h)["reader_cwd"]) for h in _USAGE_H if _usage(h).get("reader_cwd")}

def reader_cwd(h, cwd=None):
    if cwd: return cwd
    d = READER_CWD.get(h)
    return d if d and os.path.isdir(d) else None

def read_usage(h, capture=None, now=None, cwd=None, cancel=None):
    """One usage reading for harness h from its own CLI. Never invents numbers."""
    now = now or datetime.now().astimezone()
    cwd = reader_cwd(h, cwd)
    if h not in PARSERS:
        return {"harness": h, "source": "none: no CLI on router host", "read_at": now.isoformat(), "parse_ok": False,
                "state": UNKNOWN, "pools": {}, "primary": None}
    capture = capture or (lambda argv, keys: pty_capture(argv, keys, cwd=cwd, cancel=cancel))
    try:
        text = capture(C.wrap(h, [_bins()[h]], tty=True), "/usage")
    except Exception as e:  # a failed read is flagged, never 0
        return {"harness": h, "source": SOURCE[h], "read_at": now.isoformat(), "parse_ok": False, "state": "read_error",
                "error": type(e).__name__, "pools": {}, "primary": None}
    pools = PARSERS[h](text, now)
    prim = PRIMARY[h]
    ok = prim in pools and isinstance(pools[prim].get("remaining_pct"), (int, float))
    # in/out/$ only grok's /usage prints (session block); cursor and agy print none -> unknown, never estimated.
    sess = parse_grok_session(text) if h == "grok-build" else {}
    r = {"harness": h, "source": SOURCE[h], "read_at": now.isoformat(), "parse_ok": ok,
         "state": "read" if ok else "parse_failed", "pools": pools, "primary": prim if ok else None,
         "session": sess or None}
    if not ok:  # diagnostics only (log-only): screen size and a short tail with emails masked
        tail = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "<email>", re.sub(r"[ \t]+", " ", (text or "")[-700:]))
        r["screen_len"] = len(text or ""); r["screen_tail"] = tail
    return r

# ---------- per-request token scrape (fallback) ----------
TOKEN_KEYS = ("input", "output", "cached", "reasoning", "cost")

TICKS_PER_USD = 1e10  # INFERRED from one matched pair (grok usage costUsdTicks 110452400 vs /usage Cost $0.0110); not printed by grok

def ticks_cost(ticks):
    return {"usd_ticks": ticks, "usd": round(ticks / TICKS_PER_USD, 6), "scale": "1e10 ticks/$", "scale_flag": "INFERRED"}

def scrape_tokens(h, stdout, extra=None):
    """Tokens the CLI printed for this request. Unprinted fields are "unknown"."""
    t = {k: UNKNOWN for k in TOKEN_KEYS}; model = UNKNOWN
    if h == "cursor":
        try:
            j = json.loads((stdout or "").strip().splitlines()[-1])
            u = j.get("usage") or {}
            for k, src in (("input", "inputTokens"), ("output", "outputTokens"), ("cached", "cacheReadTokens")):
                if isinstance(u.get(src), int): t[k] = u[src]
        except (ValueError, IndexError, AttributeError):
            pass
    elif h == "grok-build" and isinstance(extra, dict):
        s = extra.get("request") or extra.get("session") or {}   # persistent session: this request's turns
        for k, src in (("input", "inputTokens"), ("output", "outputTokens"), ("cached", "cachedReadTokens"), ("reasoning", "reasoningTokens")):
            if isinstance(s.get(src), int): t[k] = s[src]
        if isinstance(s.get("costUsdTicks"), int): t["cost"] = ticks_cost(s["costUsdTicks"])
        model = s.get("primaryModelId") or UNKNOWN
    return t, model

def _iso_ts(v):
    v = re.sub(r"(\.\d{6})\d+", r"\1", v.replace("Z", "+00:00"))
    return datetime.fromisoformat(v).timestamp()

GROK_SESSIONS = os.path.expanduser("~/.grok/sessions")

def grok_session_reply(session_id, cwd, root=None):
    """the clean grok-build reply = the last assistant message after the last user turn in the CLI's own
    chat_history.jsonl for the verified session id (read-only). None when absent; caller falls back to clean_tui_reply."""
    from urllib.parse import quote
    if not session_id or not cwd or not re.fullmatch(r"[0-9a-f-]{36}", session_id): return None
    f = os.path.join(root or GROK_SESSIONS, quote(os.path.realpath(cwd), safe=""), session_id, "chat_history.jsonl")
    try:
        rows = [json.loads(l) for l in open(f) if l.strip()]
    except (OSError, ValueError):
        return None
    last = None
    for r in rows:
        if not isinstance(r, dict): continue
        if r.get("type") == "user": last = None
        elif r.get("type") == "assistant" and isinstance(r.get("content"), str) and r["content"].strip() and not r.get("tool_calls"):
            last = r["content"].strip()
    return last

def grok_session_usage(run=subprocess.run, after_ts=None, cwd=None):
    """(e) `grok sessions list` is scoped to the cwd (sessions/<encoded cwd>): run it in the session's own cwd, take the
    newest id (first data row), then `grok usage <id>`. Verified: the JSON's sessionId must equal the listed id and it
    must be updated at/after after_ts. "request" sums the turns that ended at/after after_ts (this request in a
    persistent session). Returns dict or None (never invents)."""
    G = _bins()["grok-build"]
    try:
        ls = run(C.wrap("grok-build", [G, "sessions", "list"]), capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, cwd=cwd).stdout
        sid = re.search(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s", ls, re.M)
        if not sid: return None
        u = run(C.wrap("grok-build", [G, "usage", sid.group(1)]), capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, cwd=cwd).stdout
        j = json.loads(u)
        if j.get("sessionId") != sid.group(1): return None
        if after_ts and "updatedAt" in j and _iso_ts(j["updatedAt"]) < after_ts: return None
        if after_ts:
            ts = [t for t in j.get("turns") or [] if t.get("endedAt") and _iso_ts(t["endedAt"]) >= after_ts]
            if ts:
                keys = ("inputTokens", "outputTokens", "cachedReadTokens", "reasoningTokens", "costUsdTicks", "modelCalls")
                j["request"] = {k: sum(t.get(k) or 0 for t in ts) for k in keys}
                j["request"]["turns"] = [t.get("turnNumber") for t in ts]
                j["request"]["primaryModelId"] = ts[-1].get("primaryModelId")
        j["verified_session_id"] = sid.group(1)
        return j
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

# ---------- request wrapper + ledger ----------
def _req(t): return lambda b, p: [b if x == "{bin}" else p if x == "{prompt}" else x for x in t]
REQUEST_ARGV = {h: _req(C.harness(h)["request_argv"]) for h in C.routed() if C.harness(h).get("request_argv")}  # pinned direct request argv

def ledger_path(path=None):
    return path or os.environ.get("USAGE_LEDGER") or os.path.join(
        C.path("state_dir", "DISPATCH_STATE"), "usage-ledger.jsonl")

def _delta(pre, post):
    out = {}
    for name, a in (pre.get("pools") or {}).items():
        b = (post.get("pools") or {}).get(name)
        if b and isinstance(a.get("used_pct"), (int, float)) and isinstance(b.get("used_pct"), (int, float)):
            out[name] = {"used_pct": round(b["used_pct"] - a["used_pct"], 4)}
        else:
            out[name] = {"used_pct": UNKNOWN}
    return out

def append_row(row, path=None):
    p = ledger_path(path); os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f: f.write(json.dumps(row, sort_keys=True) + "\n")
    return p

def run_request(h, prompt, bot_id, request_id=None, reader=None, runner=None, token_extra=None, ledger=None, critical=True, cwd=None):  # critical is recorded only; nothing is throttled
    """Pinned request with pre/post usage readings and one ledger row. Fully scripted; no routing or fallback."""
    request_id = request_id or uuid.uuid4().hex[:16]
    reader = reader or (lambda x: read_usage(x, cwd=cwd))
    pre = reader(h)
    t0 = time.time()
    if runner is None:
        argv = C.wrap(h, REQUEST_ARGV[h](_bins()[h], prompt))
        rc, out = run_group(argv, 300, cwd=cwd)
    else:
        rc, out = runner(h, prompt)
    t1 = time.time()
    if token_extra is None and h == "grok-build" and runner is None:
        token_extra = grok_session_usage(after_ts=t0)
    post = reader(h)
    tokens, model = scrape_tokens(h, out, token_extra)
    row = make_row(request_id, bot_id, h, model, rc, t0, t1, tokens, pre, post, critical, out)
    append_row(row, ledger)
    alert_line(row["pool"], (post.get("pools") or {}).get(post.get("primary") or "", {}).get("used_pct"),
               log=os.path.join(os.path.dirname(ledger_path(ledger)), "usage-alerts.log"))
    return row

REPORT_STEP = {h: _usage(h).get("report_step") for h in _USAGE_H}  # smallest % step each CLI prints

def primary_slot(reading):
    """Primary meter as {state, remaining_pct} for dispatch.log; unknown stays None, never 0."""
    prim = reading.get("primary") if isinstance(reading, dict) else None
    v = ((reading.get("pools") or {}).get(prim) or {}).get("remaining_pct") if prim else None
    return {"state": reading.get("state", UNKNOWN) if isinstance(reading, dict) else UNKNOWN,
            "remaining_pct": v if isinstance(v, (int, float)) else None}

RESET_TZ_SOURCE = {h: _usage(h).get("reset_tz_source") for h in _USAGE_H}  # where each CLI's reset comes from (config)

def reset_of(reading, h):
    """Parsed reset of the primary meter as ISO with its source zone; unknown when the CLI prints none (never declared)."""
    prim = PRIMARY.get(h)
    at = ((reading.get("pools") or {}).get(prim) or {}).get("reset_at", UNKNOWN) if isinstance(reading, dict) else UNKNOWN
    if not isinstance(at, str) or not at or at == UNKNOWN:
        return {"at": UNKNOWN, "tz_source": RESET_TZ_SOURCE.get(h, "not printed"), "meter": prim}
    if len(at) == 10:  # date-only reset is read as 00:00 UTC and flagged
        return {"at": at + "T00:00:00+00:00", "precision": "date-only", "date_printed": at, "assumed": "00:00 UTC",
                "tz_source": RESET_TZ_SOURCE.get(h), "utc_offset": "+00:00", "meter": prim}
    return {"at": at, "precision": "minute", "tz_source": RESET_TZ_SOURCE.get(h), "utc_offset": at[-6:], "meter": prim}

def latest_reset(h, path=None):
    """(found, reset_iso_or_unknown) from the newest parsed ledger row for h; (False, None) when no CLI read exists."""
    for r in reversed(read_ledger(path)):
        if r.get("harness") == h and r.get("parse_ok") and isinstance(r.get("reset"), dict):
            return True, r["reset"].get("at", UNKNOWN)
    return False, None

def reply_digest(text):
    import hashlib
    b = (text or "").encode("utf-8", "replace")
    return {"sha256": hashlib.sha256(b).hexdigest(), "len": len(b)}

def printed_total(tokens):
    """input+output exactly as printed, else unknown. Never estimated."""
    if not isinstance(tokens, dict): return UNKNOWN
    a, b = tokens.get("input"), tokens.get("output")
    return a + b if isinstance(a, int) and isinstance(b, int) else UNKNOWN

def make_row(request_id, bot_id, h, model, rc, t0, t1, tokens, pre, post, critical, out):
    parse_ok = bool(pre.get("parse_ok") and post.get("parse_ok"))
    delta = _delta(pre, post) if parse_ok else UNKNOWN
    sd = session_delta(pre, post)
    if isinstance(sd.get("input"), int) and isinstance(sd.get("output"), int):  # /usage in/out/$ delta is the exact usage
        tokens = dict(tokens or {}, usage_screen_delta=sd)
        total = sd["input"] + sd["output"]
    else:
        total = printed_total(tokens)
    prim = PRIMARY.get(h)
    pd = delta.get(prim, {}).get("used_pct", UNKNOWN) if isinstance(delta, dict) and prim else UNKNOWN
    if total != UNKNOWN and isinstance(sd.get("input"), int):
        usage = {"basis": "usage_screen_in_out_usd_delta", "exact": True, "tokens": total, "input": sd["input"],
                 "output": sd["output"], "cost_usd": sd["cost_usd"]}
    elif total != UNKNOWN:
        usage = {"basis": "printed_tokens", "exact": True, "tokens": total}
    elif isinstance(pd, (int, float)):
        usage = {"basis": "pool_pct_delta", "exact": False, "coarse": True, "used_pct_delta": pd, "step_pct": REPORT_STEP.get(h)}
    else:
        usage = {"basis": UNKNOWN, "exact": False}
    return {"ts": datetime.fromtimestamp(t1).astimezone().isoformat(), "request_id": request_id, "bot_id": bot_id,
            "harness": h, "pool": HARNESS_POOL.get(h, h), "model": model, "rc": rc, "duration_s": round(t1 - t0, 3),
            "tokens": tokens, "request_usage": usage, "reset": reset_of(post if post.get("parse_ok") else pre, h), "pre": pre, "post": post, "delta": delta,
            "parse_ok": parse_ok, "flagged": None if parse_ok else "unreadable usage: pre/post not parsed (not recorded as 0)",
            "critical": critical, "reply": reply_digest(out)}

def batch_match(rows, pool, tokens_per_pct=None):
    """Batch match: cumulative pool delta (first pre -> last post, primary meter) vs summed printed tokens.
    tokens_per_pct is the pool size in tokens per 1% and is never printed by any CLI: it must be declared (by the owner);
    without it the result is unknown, not a guess. Flagged when |gap| > one reporting step."""
    rs = [r for r in rows if r.get("pool") == pool and r.get("parse_ok")]
    if not rs: return {"pool": pool, "status": UNKNOWN, "reason": "no parsed rows", "flagged": None}
    h = rs[0]["harness"]; prim = PRIMARY.get(h); step = REPORT_STEP.get(h)
    a = rs[0]["pre"]["pools"].get(prim, {}).get("used_pct"); b = rs[-1]["post"]["pools"].get(prim, {}).get("used_pct")
    cum = round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else UNKNOWN
    tots = [printed_total(r.get("tokens")) for r in rs]
    tok = UNKNOWN if any(t == UNKNOWN for t in tots) else sum(tots)
    out = {"pool": pool, "rows": len(rs), "cum_delta_pct": cum, "printed_tokens": tok, "step_pct": step}
    if tok == UNKNOWN:
        return dict(out, status="coarse", reason="tokens not printed: % delta only", flagged=None)
    if cum == UNKNOWN or not tokens_per_pct:  # pool size stays UNKNOWN; the check is coarse (% delta only)
        return dict(out, status="coarse", pool_size=UNKNOWN, reason="pool size UNKNOWN: % delta only, no conversion, no estimate", flagged=None)
    expect = tok / float(tokens_per_pct)
    gap = round(cum - expect, 4)
    bad = abs(gap) > step
    return dict(out, status="mismatch" if bad else "match", expected_pct=round(expect, 4), gap_pct=gap,
                flagged=("gap %.4f%% > step %.2f%%" % (abs(gap), step)) if bad else None)

# ---------- persistent grok session ----------
class SessionDied(RuntimeError):
    pass

class GrokSession:
    """One interactive grok TUI kept open for router requests: /usage before, the request, /usage after, all in
    the same session, so the session in/out/$ delta is this request's. If it dies it is restarted and `restarts` goes up;
    a request that spans a restart is flagged not-exact (never 0). spawn is injectable for tests."""
    WORKED_RE = re.compile(r"Worked for \d")
    def __init__(self, cwd=None, argv=None, boot_s=9.0, spawn=None):
        self.cwd, self.argv, self.boot_s = cwd, argv or C.wrap("grok-build", [_bins()["grok-build"]], tty=True), boot_s
        self._spawn = spawn or self._pty_spawn
        self.pid = self.fd = None; self.buf = bytearray(); self.restarts = -1; self.fenced = False; self.t_start = None
    def _pty_spawn(self):
        env = dict(os.environ, TERM="xterm-256color", COLUMNS="200", LINES="50")
        pid, fd = pty.fork()
        if pid == 0:
            if self.cwd: os.chdir(self.cwd)
            try: os.execve(self.argv[0], self.argv, env)
            finally: os._exit(127)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        return pid, fd
    def alive(self):
        if self.pid is None: return False
        try: return os.waitpid(self.pid, os.WNOHANG) == (0, 0)
        except ChildProcessError: return False
    def pump(self, sec, until=None, mark=0):
        end = time.time() + sec
        while time.time() < end:
            if self.fd is None: raise SessionDied("session closed")
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if not r:
                if until and until(screen_text(bytes(self.buf[mark:]))): return True
                continue
            try: d = os.read(self.fd, 65536)
            except OSError: d = b""
            if not d: return bool(until and until(screen_text(bytes(self.buf[mark:]))))
            self.buf.extend(d)
            for q, a in REPLIES:
                if q in d: os.write(self.fd, a)
        return bool(until and until(screen_text(bytes(self.buf[mark:]))))
    def start(self):
        self.close(); self.buf = bytearray()
        self.pid, self.fd = self._spawn(); self.restarts += 1; self.fenced = False; self.t_start = time.time()
        self.pump(self.boot_s)
        if TRUST_RE.search(screen_text(bytes(self.buf))):
            os.write(self.fd, b"\x1b"); self.close()
            raise TrustPromptRefused("grok asked to trust %s; not answered" % (self.cwd or os.getcwd()))
        return self
    def ensure(self):
        if not self.alive(): self.start()
        return self
    def _keys(self, keys, settle=1.5):
        mark = len(self.buf)
        os.write(self.fd, keys.encode()); self.pump(settle, mark=mark)
        if TRUST_RE.search(screen_text(bytes(self.buf[mark:]))):  # re-checked right before Enter
            os.write(self.fd, b"\x1b"); raise TrustPromptRefused("trust prompt in grok session; Enter not sent")
        return mark
    last_reading = None  # newest read_usage() result from this session (pre-read for the next request, non-blocking)
    def capture_usage(self, argv=None, keys="/usage"):
        """read_usage capture: /usage in THIS session, screen text since the keys, then Esc."""
        self.ensure(); gen = self.restarts
        def w(data):  # (b) a reader from an older (fenced) session generation never types into the new one
            if self.fenced or gen != self.restarts or self.fd is None: raise SessionDied("fenced")
            os.write(self.fd, data)
        w(b"\x1b"); self.pump(1.0)                              # (d) dismiss whatever is open first
        m0 = len(self.buf); w(keys.encode()); self.pump(1.5)
        if TRUST_RE.search(screen_text(bytes(self.buf[m0:]))):  # re-checked right before Enter
            w(b"\x1b"); raise TrustPromptRefused("trust prompt in grok session; Enter not sent")
        w(b"\r"); mark = len(self.buf)                         # fresh: only bytes after Enter count
        fresh = lambda t: ("Weekly limit" in t) and bool(re.search(r"Cost:\s*\$[\d.]+|no model calls yet", re.sub(r"\s+", " ", t[t.rfind("Weekly limit"):])))
        self.pump(20.0, until=fresh, mark=mark); self.pump(1.0)
        text = screen_text(bytes(self.buf[mark:]))
        k = text.rfind("Weekly limit")
        text = text[k:] if k >= 0 else text                    # newest panel only, never stale text
        w(b"\x1b"); self.pump(1.0)
        return text
    def ask(self, prompt, timeout=600):
        """Send one request (bracketed paste, then Enter). Returns (rc, screen_text_since_send). rc 'timeout' if no finish marker."""
        if not self.alive(): raise SessionDied("grok session not running")
        mark = self._keys("\x1b[200~" + prompt + "\x1b[201~")
        os.write(self.fd, b"\r")
        done = self.pump(timeout, until=lambda t: bool(self.WORKED_RE.search(t)), mark=mark)
        if not done and not self.alive(): raise SessionDied("grok session died during the request")
        return (0 if done else "timeout"), screen_text(bytes(self.buf[mark:]))
    def fence(self):
        """(b) after a read timed out: kill this session so a late reader cannot type into the next job; it restarts on next use."""
        self.fenced = True; self.close()
    def close(self):
        if self.pid is None: return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(self.pid, sig); time.sleep(0.5)
            except ProcessLookupError: break
        try: os.waitpid(self.pid, 0)
        except ChildProcessError: pass
        try: os.close(self.fd)
        except OSError: pass
        self.pid = self.fd = None

_GROK_SESSION = None
def grok_router_session(cwd=None):
    """The router's persistent grok session (one per router process)."""
    global _GROK_SESSION
    if _GROK_SESSION is None: _GROK_SESSION = GrokSession(cwd=cwd)
    return _GROK_SESSION

def read_session_usage(sess, now=None):
    r = read_usage("grok-build", capture=sess.capture_usage, now=now)
    if r.get("parse_ok"): sess.last_reading = r   # (a) a failed read is logged, never saved or reused
    return r

def grok_session_call(sess, prompt, timeout=600):
    """One router request through the persistent session. A dead session is restarted once and the call flagged."""
    r0 = sess.restarts
    try:
        rc, text = sess.ensure().ask(prompt, timeout)
    except SessionDied:
        sess.start()
        rc, text = sess.ask(prompt, timeout)
    # a first start (restarts -1 -> 0) is not a restart; any later start inside this call is (flagged not-exact)
    return rc, text, sess.restarts > max(r0, 0)

# ---------- rollups, alerts, throttle ----------
def read_ledger(path=None):
    p = ledger_path(path); rows = []
    if not os.path.exists(p): return rows
    with open(p, encoding="utf-8") as f:
        for line in f:
            try: rows.append(json.loads(line))
            except ValueError: rows.append({"event": "corrupt_row"})
    return rows

def _pool_used(rows, pool):
    """Latest parsed post reading (primary) for the pool, else unknown."""
    for r in reversed(rows):
        if r.get("pool") == pool and r.get("parse_ok"):
            post = r["post"]; prim = post.get("primary")
            v = (post.get("pools") or {}).get(prim, {}).get("used_pct")
            if isinstance(v, (int, float)): return v, post.get("read_at"), (post.get("pools") or {}).get(prim, {}).get("reset_at", UNKNOWN)
    return UNKNOWN, None, UNKNOWN

def alert_level(used):
    if not isinstance(used, (int, float)): return UNKNOWN
    lvl = None
    for a in ALERT_PCTS:
        if used >= a: lvl = a
    return lvl

def alert_line(pool, used, log=None, now=None):
    """75%/90% alert: append one log line and return it; never blocks anything. Unknown -> no line."""
    lvl = alert_level(used)
    if lvl in (None, UNKNOWN): return None
    line = "%s ALERT pool=%s used=%.2f%% level=%d%%" % ((now or datetime.now().astimezone()).isoformat(), pool, used, lvl)
    p = log or os.path.join(os.path.dirname(ledger_path()), "usage-alerts.log")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f: f.write(line + "\n")
    return line

def rollup(rows, period="daily", now=None):
    now = (now or datetime.now()).astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly": start -= timedelta(days=start.weekday())
    out = {"period": period, "since": start.isoformat(), "pools": {}}
    for pool in POOLS:
        prs = [r for r in rows if r.get("pool") == pool and r.get("ts") and datetime.fromisoformat(r["ts"]) >= start]
        tok = {k: 0 for k in TOKEN_KEYS if k != "cost"}; unk = {k: 0 for k in tok}
        for r in prs:
            for k in tok:
                v = (r.get("tokens") or {}).get(k)
                if isinstance(v, int): tok[k] += v
                else: unk[k] += 1
        used, at, reset = _pool_used(rows, pool)
        out["pools"][pool] = {"requests": len(prs),
                              "flagged_unreadable": len([r for r in prs if r.get("parse_ok") is False]),
                              "tokens_known_sum": tok, "tokens_unknown_rows": unk,
                              "used_pct": used, "reset_at": reset, "reading_at": at, "alert": alert_level(used),
                              "routed": POOLS[pool]["routed"]}
    return out

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="router usage layer: read | run | rollup")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("read"); r.add_argument("harness")
    q = sp.add_parser("run"); q.add_argument("harness"); q.add_argument("--prompt", required=True); q.add_argument("--bot-id", required=True)
    q.add_argument("--request-id"); q.add_argument("--ledger"); q.add_argument("--non-critical", action="store_true", help="recorded only; no throttle")
    u = sp.add_parser("rollup"); u.add_argument("--period", choices=["daily", "weekly"], default="daily"); u.add_argument("--ledger")
    x = ap.parse_args(argv)
    if x.cmd == "read": print(json.dumps(read_usage(x.harness), indent=1))
    elif x.cmd == "run": print(json.dumps(run_request(x.harness, x.prompt, x.bot_id, x.request_id, ledger=x.ledger, critical=not x.non_critical, cwd=os.getcwd()), indent=1))
    else: print(json.dumps(rollup(read_ledger(x.ledger), x.period), indent=1))

if __name__ == "__main__": main()
