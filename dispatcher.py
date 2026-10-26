#!/usr/bin/env python3
"""Dispatcher: routing, approval checks, leases, redaction and the command line.
The dispatcher itself sends nothing (live.py does), never approves a packet and never writes the GO ledger. Standard library only.

Precedence, applied in this order everywhere:
  1. hard rules: KILL file, LAN rule (LAN work -> lan.harness only), lease
     (no cap gate: a missing, null, or 0 cap is no ceiling; enabled is not a skip)
  2. score = A^0.6 * U^0.4 * H
     When every scored harness is config-gap, score = A^0.6 * H (basis ability-only).
     Ties then go to the lowest recent usage-counter count, then fallback order.
  3. fallback order (Grok Build > Cursor > AGY/Gemini) ONLY as tie-break
     (|score diff| < 1e-9) or when every score is unknown.
"""
import argparse, hashlib, json, os, re, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drconfig as C
KILL = os.environ.get("DISPATCH_KILL", os.path.join(HERE, "KILL"))
# Router build 8 / usage layer: only config harnesses with route=true are route candidates
# candidate. It keeps its own usage pool for logging only (usage_layer.POOLS).
FALLBACK = C.routed()  # config: harnesses with route=true, in file order
ROUTE_EXCLUDED = tuple(h for h, v in C.harnesses().items() if not v.get("route"))
U_DEFAULT = 0.3
# 168*((0.9/0.8)**1.5-1)*1.15/168 rounded to 0.2224, so an ability-0.8 harness with unused allowance strictly outranks an ability-0.9 harness without it.
K_STEP_REL = 0.2224
PHASE1_NOTE = "No usage snapshots: routing by ability only (U = 0.3 default)"

# L3: adapter owners. Process off is the KILL file. enabled=false is not a balancer flag.
ADAPTERS = {h: {"owner": v.get("owner", "unset"), "off": v.get("off", "KILL file")} for h, v in C.harnesses().items()}

def check_kill():
    if os.path.exists(KILL):
        print(f"KILLED: {KILL} present; refusing to act.", file=sys.stderr); sys.exit(3)

def load(p):
    with open(p) as f: return json.load(f)

def sha256f(p):
    h = hashlib.sha256()
    with open(p, "rb") as f: h.update(f.read())
    return h.hexdigest()

# ---------- redaction ----------
SHA_FIELDS = {"sha", "sha256", "packet_sha", "receipt_sha", "commit"}
SHA_RE = re.compile(r"^(?:[0-9a-f]{12}|[0-9a-f]{64})$")
LONGHEX = re.compile(r"\b[0-9a-fA-F]{20,}\b")
SECRET_RES = [re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
              re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"),
              re.compile(r"\b(?:sk|pk|ghp|gho|xox[abp]|AKIA|AIza)[-_A-Za-z0-9]{10,}")]

def redact_text(s):
    hits = 0
    for r in SECRET_RES + [LONGHEX]:
        s, n = r.subn("[REDACTED]", s); hits += n
    return s, hits

def redact(obj, key=None):
    """returns (clean, hits). sha-shaped values pass only in SHA_FIELDS."""
    if isinstance(obj, dict):
        out, h = {}, 0
        for k, v in obj.items():
            c, n = redact(v, k); out[k] = c; h += n
        return out, h
    if isinstance(obj, list):
        out, h = [], 0
        for v in obj:
            c, n = redact(v); out.append(c); h += n
        return out, h
    if isinstance(obj, str):
        if key in SHA_FIELDS and SHA_RE.match(obj): return obj, 0
        return redact_text(obj)
    return obj, 0

# ---------- lease ----------
def lease_dir(): return (os.environ.get("DISPATCH_STATE") or C.path("state_dir")) + "/leases"

def lease_acquire(job, owner, ttl=600, now=None):
    check_kill()
    now = now or time.time(); d = lease_dir(); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{job}.json")
    if os.path.exists(p):
        L = load(p)
        live = L["expiry"] > now and not L.get("kill_confirmed")
        receipt = L.get("receipt")
        # Same rule as lease_held: a receipt frees only when _receipt_frees says so.
        if live and receipt and _receipt_frees(receipt, _acquire_time(L)):
            live = False
        if live and L["owner"] != owner:
            return False, f"REFUSED: job {job} leased by {L['owner']} until {int(L['expiry'])}; reroute waits for release, expiry, or confirmed kill"
        os.remove(p)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # atomic: two racers, one wins
    except FileExistsError:
        return False, f"REFUSED: job {job} lease race lost"
    with os.fdopen(fd, "w") as f:
        json.dump({"job": job, "owner": owner, "start": now, "heartbeat": now, "expiry": now + ttl, "acquired": now}, f)
    return True, f"LEASED {job} -> {owner}"

def lease_release(job, owner, kill_confirmed=False):
    p = os.path.join(lease_dir(), f"{job}.json")
    if not os.path.exists(p): return True, "no lease"
    L = load(p)
    if kill_confirmed:
        L["kill_confirmed"] = True
        with open(p, "w") as f: json.dump(L, f)
        return True, f"KILL CONFIRMED {job}"
    if L["owner"] != owner: return False, "REFUSED: not owner"
    os.remove(p); return True, f"RELEASED {job}"

def lease_extend(job, owner, expiry, receipt=None):
    """Rewrite expiry on the job lease. A real PENDING waits until that deadline or its receipt."""
    p = os.path.join(lease_dir(), f"{job}.json")
    if not os.path.exists(p): return False, "no lease"
    L = load(p)
    if L.get("owner") != owner: return False, "REFUSED: not owner"
    L["expiry"] = expiry
    if receipt: L["receipt"] = receipt
    with open(p, "w") as f: json.dump(L, f)
    return True, f"EXTENDED {job} -> {owner} until {expiry}"

def _acquire_time(L):
    """Acquire time lease_acquire stored. `acquired` is that field; older files used `start`."""
    if isinstance(L, dict) and L.get("acquired") is not None:
        return L.get("acquired")
    if isinstance(L, dict):
        return L.get("start")
    return None

def _receipt_frees(path, acquired):
    """True only for a complete receipt of this run.

    Regular file, non-empty, mtime >= the acquire time, and a line starting with
    "Result:". OSError, TypeError, or ValueError means the receipt does not free.
    """
    try:
        if not os.path.exists(path):
            return False
        if not os.path.isfile(path):
            return False
        if os.path.getsize(path) <= 0:
            return False
        if os.path.getmtime(path) < float(acquired):
            return False
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("Result:"):
                    return True
        return False
    except (OSError, TypeError, ValueError):
        return False

def lease_held(job, now=None):
    p = os.path.join(lease_dir(), f"{job}.json")
    if not os.path.exists(p): return None
    L = load(p); now = now or time.time()
    receipt = L.get("receipt")
    # A partial, prior-run, or unreadable receipt does not free; expiry still can.
    if receipt and _receipt_frees(receipt, _acquire_time(L)):
        return None
    return L if (L["expiry"] > now and not L.get("kill_confirmed")) else None

def pending_held(job, now=None):
    """Unexpired async wait at state/pending/<job>.json, or None. Read-only: never writes or deletes.

    A receipt frees only when _receipt_frees is true for the `submitted` time
    submit_async stored. No submit time, or a partial, prior-run, or unreadable
    receipt, does not free. Held while now < deadline.
    """
    now = time.time() if now is None else now
    path = os.path.join((os.environ.get("DISPATCH_STATE") or C.path("state_dir")), "pending", f"{job}.json")
    try:
        with open(path) as f: rec = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(rec, dict) or rec.get("status") == "done":
        return None
    receipt = rec.get("receipt")
    if receipt:
        submitted = rec.get("submitted")
        if submitted is not None and _receipt_frees(receipt, submitted):
            return None
    try:
        if not now < rec["deadline"]: return None
    except (TypeError, KeyError):
        return None
    return rec

def _log_iso(t):
    """ISO-8601 with a numeric offset, seconds resolution."""
    if isinstance(t, datetime):
        dt = t
    elif isinstance(t, str):
        dt = datetime.fromisoformat(t)
    elif isinstance(t, (int, float)) and not isinstance(t, bool):
        dt = datetime.fromtimestamp(t)
    else:
        dt = datetime.now()
    if dt.tzinfo is None:
        dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt.replace(microsecond=0).isoformat(timespec="seconds")

def _usage_slot(rec):
    """dispatch.log token: a remaining percent, or config-gap, or missing."""
    if isinstance(rec, dict):
        state = rec.get("state")
        if state in ("config-gap", "missing"):
            return state
        pct = rec.get("remaining_pct")
        if not isinstance(pct, bool) and isinstance(pct, (int, float)):
            if isinstance(pct, float) and pct.is_integer():
                return str(int(pct))
            return str(pct)
    return "missing"

def dispatch_log_line(job, h, t_start, t_end, before, after, state_dir=None):
    """Append one live-call line to <DISPATCH_STATE>/dispatch.log and return it."""
    if state_dir is None:
        state_dir = (os.environ.get("DISPATCH_STATE") or C.path("state_dir"))
    line = "job=%s harness=%s start=%s end=%s before=%s after=%s" % (
        job, h, _log_iso(t_start), _log_iso(t_end), _usage_slot(before), _usage_slot(after))
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "dispatch.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line

# ---------- usage ----------
def _usage_writer():
    """Lazy import so dispatcher load does not flip usage_writer's bytecode flag."""
    import usage_writer as UW
    return UW

def _writer_config_gap(h):
    """True when usage_writer would report config-gap for h (declarations unset).

    File absence is not the signal. Unknown harness names are not the writer's gap.
    """
    UW = _usage_writer()
    if h not in UW.HARNESSES:
        return False
    return bool(UW.missing(UW.fields_for(h, None)))

def _observed_burn_pct_per_h(h, udir, now, snap):
    """Observed burn in percent per hour from <udir>/<h>.counter.json, or None.

    Same formula as usage_writer.write_snapshot: used_pct / elapsed_h, elapsed
    floored at MIN_ELAPSED_H. Allowance comes from the writer declaration when
    it is set (used_pct = count / allowance * 100). A missing counter is not a
    rate. A positive count with no allowance uses the snapshot remaining only
    when that remaining is a consistent used-fraction; otherwise None. Never
    substitutes 0 for a missing rate. count == 0 is a real observed 0 (0% of
    any positive allowance).
    """
    UW = _usage_writer()
    if not udir or not os.path.isdir(udir):
        return None
    counter = UW.read_json(UW.counter_path(udir, h))
    if not isinstance(counter, dict):
        return None
    if "count" not in counter or "window_start" not in counter:
        return None
    try:
        count = int(counter["count"])
    except (TypeError, ValueError):
        return None
    if isinstance(counter["count"], bool) or count < 0:
        return None
    try:
        start = UW.coerce_dt(counter["window_start"])
        now_dt = UW.coerce_dt(now)
    except (TypeError, ValueError):
        return None
    elapsed_h = (now_dt - start).total_seconds() / 3600.0
    if elapsed_h < UW.MIN_ELAPSED_H:
        elapsed_h = UW.MIN_ELAPSED_H
    fields = UW.fields_for(h, None)
    allowance = fields.get("allowance")
    if not UW.is_unset(allowance) and not isinstance(allowance, bool):
        try:
            allowance_f = float(allowance)
        except (TypeError, ValueError):
            allowance_f = 0.0
        if allowance_f > 0:
            return (count / allowance_f * 100.0) / elapsed_h
    if count == 0:
        return 0.0
    if not isinstance(snap, dict) or "remaining_pct" not in snap:
        return None
    try:
        rem_pct = float(snap["remaining_pct"])
    except (TypeError, ValueError):
        return None
    used_pct = 100.0 - rem_pct
    if used_pct <= 0:
        return None
    return used_pct / elapsed_h

def _cli_reset(h):
    try:
        import usage_layer as UL
        return UL.latest_reset(h)
    except Exception:
        return False, None

def usage_for(h, udir, now=None):
    """Waste score over the whole window. Returns (U, state).

    expected_unused = remaining - burn * hours_to_reset, all fractions.
    A burn key in the snapshot is used as given. A missing burn is never
    replaced with 0: the usage counter supplies an observed pct/h, and if
    that rate does not exist the waste term is omitted (neutral, U = rem).

    rem is the remaining fraction. eu is expected_unused. tl is hours_to_reset
    floored at 0. w is window_len_h. When the burn is known and eu > 0 and w > 0:
        prox = min(1, max(0, 1 - tl/w))
        U = rem * (1 + K_STEP_REL + eu * prox)
    Otherwise U = rem (neutral). No min(1, ...) clamp. Distinct positive
    remainders stay distinct. Age > 6h and <= 24h multiplies that U by 0.5.
    Age > 24h is unknown U 0.3, as before.

    K_STEP_REL = 0.2224. rounded
    168 * ((0.9/0.8)**1.5 - 1) * 1.15 / 168 to that value. Score is
    A^0.6 * U^0.4, so U_A/U_B must exceed (0.9/0.8)**1.5 ≈ 1.1932 for an
    ability-0.8 wasting harness to beat an ability-0.9 harness that is not
    wasting. A term that only multiplies by expected_unused vanishes as
    expected_unused → 0+, so it cannot clear a fixed ratio for every positive
    waste. The step does: at any eu > 0 the multiplier is already
    1 + 0.2224 before the prox term, which is above that bar. The 1.15 factor
    inside the rounding keeps that ordering strict. prox is how close reset is inside
    the window (1 at reset, 0 when a full window remains), so the same waste
    scores higher nearer the reset and window length changes U.

    Writer config-gap (declared allowance / window_len_h / reset_at unset)
    writes no snapshot. When the usage directory exists and the writer is in
    that gap, state is "config-gap" and U is 0, which is <= 0.3 and strictly
    below every fresh positive remainder (those have U >= rem > 0). A missing
    directory stays "unknown" / 0.3 so callers that never created a usage dir
    keep the pre-phase result. A snapshot the writer actually wrote (state
    "counter") is scored from its fields and is not config-gap.
    """
    if not udir or not os.path.isdir(udir):
        p = os.path.join(udir, "%s.json" % h) if udir else None
        if not p or not os.path.exists(p):
            return U_DEFAULT, "unknown"
    else:
        p = os.path.join(udir, "%s.json" % h)
        if not os.path.exists(p):
            if _writer_config_gap(h):
                return 0.0, "config-gap"
            return U_DEFAULT, "unknown"
    s = load(p); now = now or time.time()
    age_h = (now - datetime.fromisoformat(s["read_at"]).timestamp()) / 3600.0
    if age_h > 24:
        return U_DEFAULT, "unknown(>24h)"
    rem = float(s["remaining_pct"]) / 100.0
    # K_STEP_REL keeps that ordering strict: 1 + 0.2224 > (0.9/0.8)**1.5. See the docstring.
    if "burn_pct_per_h" in s and s["burn_pct_per_h"] is not None and not isinstance(s["burn_pct_per_h"], bool):
        burn_pct = float(s["burn_pct_per_h"])
    else:
        burn_pct = _observed_burn_pct_per_h(h, udir, now, s)
    # the reset is the one parsed from the CLI's own /usage (newest parsed ledger row).
    # cursor's date-only reset is stored as 00:00 UTC (flagged date-only) and scored; a legacy bare date or unknown scores waste 0 (U = rem). Without any CLI read the snapshot's reset stands
    # (production declarations are unset, so that path is config-gap; only test fixtures declare one).
    cli_found, cli_reset = _cli_reset(h)
    if cli_found and (not cli_reset or cli_reset == "unknown" or len(cli_reset) == 10):  # date-only: no time printed
        burn_pct = None
    reset_iso = cli_reset if cli_found and burn_pct is not None else s["reset_at"]
    if burn_pct is None:
        u = rem  # neutral: no waste term, and not a fake burn of 0
    else:
        tl = (datetime.fromisoformat(reset_iso).timestamp() - now) / 3600.0
        if tl < 0:
            tl = 0.0
        eu = rem - (burn_pct / 100.0) * tl
        if eu > 0:
            w = float(s["window_len_h"])
            if w > 0:
                # prox is nearness to reset inside the window.
                prox = min(1.0, max(0.0, 1.0 - tl / w))
                u = rem * (1.0 + K_STEP_REL + eu * prox)
            else:
                u = rem
        else:
            u = rem
    if age_h > 6:
        return u * 0.5, "stale"
    return u, "fresh"

# ---------- route ----------
# LAN detection: declared Placement / Network placement / Hosts / Host lines and the
# Objective line only. No body scan. Role names come from the IP schema at runtime.
PLACEMENT_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?\**(?P<key>Hosts?|Network placement|Placement)\**\s*:\s*(?P<val>.+)$")
NEG_RE = re.compile(r"(?i)\b(no|not|never|nor|without|don't|do not|barred|forbid\w*|untouched|stays? (?:off|flagged))\b")
NEXT_SEC = re.compile(r"^\s*(?:#+\s|\d+\.\s|[A-Z][A-Za-z /+-]{2,40}:)")
OBJ_RE = re.compile(r"(?im)^\s*(?:\d+\.\s*)?\**Objective\**\s*:\s*(.+)$")
# Word-bounded explicit token: "LAN", which also covers "on the LAN" and "LAN host(s)".
_LAN_TOKEN_RE = re.compile(r"(?i)\bLAN\b")
_FIELD_OFF_RE = re.compile(r"(?i)^\s*(?:none|n/a|local)\b")
_HOSTS_CLAUSE_RE = re.compile(r"(?i)\bHosts?\s*:\s*(.+)$")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
# Address-shaped spans are ignored. They are not a LAN signal and are never returned.
_ADDR_RE = re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){2,}[0-9a-f:]*\b|\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Not roles. Never match, even when a schema label normalises to one of these.
_BARE = frozenset({"router", "switch", "firewall", "homelab"})
_SKIP_CELL = frozenset({"\u2014", "-", "\u2013"})
_SECTION_RE = re.compile(r"(?i)^(?:#{1,6}\s+)?(?:\*\*)?Static assignments\b")
_NEXT_HEAD_RE = re.compile(r"^#{1,6}\s+\S")
_SCHEMA_DEFAULT = C.path("ip_schema")
LAN_HARNESS = C.setting("lan", "harness")  # config lan.harness: the only harness allowed for LAN work (None = LAN work routes nowhere)
_LAN_ROLE_CACHE = {"key": None, "names": frozenset()}

def _norm_name(s):
    """Lowercase, drop non-alphanumerics, strip leading zeros from each digit run."""
    s = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return re.sub(r"\d+", lambda m: str(int(m.group())), s)

def _split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]

def _is_sep_row(line):
    cells = [c for c in _split_row(line) if c != ""]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)

def _fqdn_label(cell):
    cell = re.sub(r"[*_`]", "", cell or "").strip()
    if not cell or cell in _SKIP_CELL or cell.lower() == "tbd" or _ADDR_RE.search(cell):
        return None
    label = cell.split(".", 1)[0].strip()
    if not label or label.lower() == "tbd" or label in _SKIP_CELL:
        return None
    name = _norm_name(label)
    if not name or name in _BARE:
        return None
    return name

def _roles_from_schema(text):
    """First dot-label of each FQDN cell in the Static assignments table. No addresses."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _SECTION_RE.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return set()
    body = []
    for line in lines[start:]:
        if _NEXT_HEAD_RE.match(line.strip()):
            break
        body.append(line)
    i = 0
    while i < len(body):
        line = body[i].strip()
        if line.startswith("|"):
            headers = [re.sub(r"[*_`]", "", c).strip().lower() for c in _split_row(line)]
            if "fqdn" in headers:
                idx = headers.index("fqdn")
                i += 1
                if i < len(body) and _is_sep_row(body[i]):
                    i += 1
                roles = set()
                while i < len(body) and body[i].strip().startswith("|"):
                    row = _split_row(body[i])
                    if idx < len(row):
                        name = _fqdn_label(row[idx])
                        if name:
                            roles.add(name)
                    i += 1
                return roles
        i += 1
    return set()

def lan_roles(path=None):
    """Normalised lab-host role names from the IP schema. Empty if it is missing or yields none.
    Cached per (path, mtime). Never prints or returns addresses."""
    path = path or os.environ.get("DISPATCH_IP_SCHEMA", _SCHEMA_DEFAULT)
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    if _LAN_ROLE_CACHE["key"] != key:
        names = frozenset()
        if key[1] is not None:
            try:
                with open(path, encoding="utf-8") as f:
                    names = frozenset(_roles_from_schema(f.read()))
            except (OSError, UnicodeError):
                names = frozenset()
        _LAN_ROLE_CACHE["key"] = key
        _LAN_ROLE_CACHE["names"] = names
    return set(_LAN_ROLE_CACHE["names"])

def _hosts_tokens(val):
    cleaned = _ADDR_RE.sub(" ", val or "")
    out = []
    for tok in _WORD_RE.findall(cleaned):
        name = _norm_name(tok)
        if name:
            out.append(name)
    return out

def _field_is_lan(key, val, roles):
    val = (val or "").strip()
    if _FIELD_OFF_RE.match(val):
        return False
    if _LAN_TOKEN_RE.search(val):
        return True
    if not re.fullmatch(r"(?i)hosts?", key or ""):
        return False
    toks = _hosts_tokens(val)
    if roles:
        return any(t in roles for t in toks)
    return any(t not in _BARE for t in toks)  # schema missing or empty: fail closed

def is_lan(text):
    """LAN only from a declared Placement / Network placement / Hosts / Host line, or the Objective line.
    Explicit LAN token, or a Hosts/Host value that names a lan_roles() name. No body scan.
    A value starting none / n/a / local is not LAN. A missing or empty schema fails closed on Hosts."""
    roles = lan_roles()
    for m in PLACEMENT_RE.finditer(text or ""):
        if _field_is_lan(m.group("key"), m.group("val"), roles):
            return True
    for obj in OBJ_RE.findall(text or ""):
        if _LAN_TOKEN_RE.search(obj):
            return True
        for hm in _HOSTS_CLAUSE_RE.finditer(obj):
            if _field_is_lan("Hosts", hm.group(1), roles):
                return True
    return False

TYPES = {"browser": r"(?i)\bbrowser\b|\bweb ?pages?\b|\bwebsite\b|\bsite playbook\b", "research": r"(?i)research|eval|read-only|summar(?:y|ies|ize|ise)",
         "refactor": r"(?i)refactor|repo-wide", "code_edit": r"(?i)code|build|script|fix"}

CLS_SKIP = re.compile(r"(?i)^\s*(?:#+\s*|\d+\.\s*|[-*]\s*)?\**(Basis|Input|Inputs|Boundaries|Gates|NEVER|NOT covered|Out of scope|Anti-jobs|Scars|Reviewer notes|Sources?)\b")

# Negated clause: from the negation word up to the end of the clause (. ; ! ? or end of line).
# Only the clause is dropped, so "Read-only: do not edit any file." keeps its "Read-only" signal.
NEG_CLAUSE_RE = re.compile(r"(?i)\b(?:no|not|never|nor|without|don't|do not|does not|must not|cannot|can't)\b[^.;!?\n]*")
# Whole-line markers: a line that says a thing is barred/untouched carries no positive work signal.
NEG_LINE_RE = re.compile(r"(?i)\b(barred|forbid\w*|untouched|stays? (?:off|flagged))\b")

def strip_negated(line):
    """Drop negated clauses ("do not fix the build script") but keep the rest of the line."""
    if NEG_LINE_RE.search(line): return ""
    return NEG_CLAUSE_RE.sub(" ", line)

def classify_text(text):
    """CA 2: classify on the declared Objective only (first sentence-ish line). No Objective ->
    whole text minus Basis/Input/Boundaries/NOT covered/Scars sections, so a packet that merely
    discusses 'browser jobs' is not routed as browser."""
    m = OBJ_RE.findall(text)
    if m: return " ".join(strip_negated(x) for x in m)
    keep, skip = [], False
    for line in text.splitlines():
        if CLS_SKIP.match(line): skip = True; continue
        if skip and NEXT_SEC.match(line): skip = False
        if not skip:
            kept = strip_negated(line)
            if kept.strip(): keep.append(kept)
    return "\n".join(keep)

def classify(text):
    t0 = classify_text(text)
    for t, r in TYPES.items():
        if re.search(r, t0): return t
    return "code_edit"

UNKNOWN = "UNKNOWN"

# ---------- packet corpus (excludes *.bak*) ----------
import fnmatch
def corpus(d=None, pat="*.md"):
    import glob
    d = d or C.path("packets_dir")
    return sorted(p for p in glob.glob(os.path.join(d, pat)) if not fnmatch.fnmatch(os.path.basename(p), "*.bak*"))

# Cooldown length N in seconds, unset by default; None = config-gap, no cooldown skip applied. One N for every harness and failure class.
HEALTH_COOLDOWN_S = None
FAIL_CLASSES = {"failed_retryable", "timed_out", "failed_hold:usage", "failed_hold:not_installed", "cancelled", "STALE"}
# Live fallback moves to the next harness only for these. Every other status stops.
# failed_hold:not_installed is a missing binary: an unpinned run falls back to the next harness. A pin still tries only that harness.
WALK_STATUSES = {"failed_retryable", "timed_out", "failed_hold:usage", "failed_hold:not_installed"}
HEALTH_GAP = "HEALTH_COOLDOWN_S unset, cooldown not applied"

def health_path():
    """health.json under $DISPATCH_STATE, else <dispatcher>/state. No harness_call import."""
    return os.path.join((os.environ.get("DISPATCH_STATE") or C.path("state_dir")), "health.json")

def read_health(path=None):
    """Harness -> {class, ts}. {} if the file is missing or unreadable. No subprocess."""
    path = health_path() if path is None else path
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}

def _health_has_failure(recs):
    if not isinstance(recs, dict):
        return False
    for rec in recs.values():
        if isinstance(rec, dict) and rec.get("class") in FAIL_CLASSES:
            return True
    return False

def health_skip(h, rec, now, n):
    """True when rec's class is a failure, n is set, and now is still before ts + n."""
    if n is None or not isinstance(rec, dict):
        return False
    if rec.get("class") not in FAIL_CLASSES:
        return False
    try:
        return now < rec["ts"] + n
    except (TypeError, KeyError):
        return False

def _recent_use(h, udir):
    """This harness's usage-counter count, or 0 when the counter is missing."""
    if not udir:
        return 0
    UW = _usage_writer()
    try:
        counter = UW.read_json(UW.counter_path(udir, h))
        if not isinstance(counter, dict) or "count" not in counter:
            return 0
        n = counter["count"]
        if isinstance(n, bool):
            return 0
        return int(n)
    except (OSError, TypeError, ValueError):
        return 0

def route(text, capmap, caps, udir=None, health=None, job=None, now=None, cooldown_s=None):
    from_file = health is None
    if from_file:
        health_recs = read_health()
        n = HEALTH_COOLDOWN_S if cooldown_s is None else cooldown_s
        health_now = time.time() if now is None else now
        health_gap = HEALTH_GAP if (n is None and _health_has_failure(health_recs)) else None
    else:
        health = health or {}  # caller dict: H = health.get(h, 1.0); do not read the file
    ttype = classify(text); lan = is_lan(text)
    rows, skipped = [], []
    held = (lease_held(job, now) or pending_held(job, now)) if job else None
    for h in FALLBACK:
        # No cap gate. A missing, null, or 0 cap is no ceiling, and a positive
        # daily_cap / used_today pair does not skip either. enabled is not a
        # skip and is not a score weight. caps is accepted and not consulted.
        if lan and h != LAN_HARNESS: skipped.append((h, "hard: LAN work -> %s only (config lan.harness)" % LAN_HARNESS)); continue
        if from_file:
            rec = health_recs.get(h)
            if health_skip(h, rec, health_now, n):
                skipped.append((h, "health: cooldown %s until %s" % (rec["class"], rec["ts"] + n)))
                continue
            H = 1.0
        else:
            H = health.get(h, 1.0)
        A = capmap[h].get(ttype)
        U, ustate = usage_for(h, udir, now)
        score = None if A is None else (A ** 0.6) * (U ** 0.4) * H
        rows.append({"harness": h, "A": A, "U": round(U, 3), "usage": ustate, "H": H,
                     "route_via": capmap[h].get("route_via") or C.harness(h).get("route_via", "direct"), "host": capmap[h].get("host") or C.harness(h).get("host", "local"),
                     "score": None if score is None else round(score, 4), "fb": FALLBACK.index(h)})
    # Every scored harness in config-gap: ignore the gap and score on ability alone.
    ability_only = bool(rows) and all(r["usage"] == "config-gap" for r in rows)
    if ability_only:
        for r in rows:
            A = r["A"]
            raw = None if A is None else (A ** 0.6) * r["H"]
            r["score"] = None if raw is None else round(raw, 4)
    known = [r for r in rows if r["score"] is not None]
    if known:
        if ability_only:
            rows = sorted(known, key=lambda r: (-r["score"], _recent_use(r["harness"], udir), r["fb"])) + [r for r in rows if r["score"] is None]
        else:
            rows = sorted(known, key=lambda r: (-r["score"], r["fb"])) + [r for r in rows if r["score"] is None]
        tie = len(known) > 1 and abs(rows[0]["score"] - rows[1]["score"]) < 1e-9
        if tie and ability_only and _recent_use(rows[0]["harness"], udir) != _recent_use(rows[1]["harness"], udir):
            why = "score tie -> lowest recent use"
        else:
            why = "score tie -> fallback order" if tie else "score"
    else:
        rows.sort(key=lambda r: r["fb"]); why = "all scores unknown -> fallback order"
    if held: rows = []  # No (re)route while a live lease or unexpired pending wait exists
    phase1 = all(r["usage"].startswith("unknown") for r in rows)
    out = {"task_type": ttype, "lan": lan, "lease": held, "ranked": rows, "skipped": skipped,
           "decided_by": why if not held else "hard: lease held -> no reroute", "phase1": phase1,
           "basis": "ability-only" if ability_only else "usage"}
    if from_file:
        out["health_gap"] = health_gap
    return out

# ---------- dry-run ----------
def find_go(ledger, sha12, name):
    with open(ledger) as f:
        for i, l in enumerate(f, 1):
            if "| GO |" in l and re.search(r"(?<![0-9a-f])" + re.escape(sha12) + r"(?![0-9a-f])", l) and re.search(r"(?<![\w.-])" + re.escape(name.replace(".md", "")) + r"(?:\.md)?(?![\w-])", l): return i, l.strip()
    return None, None

def find_runs(runs, path):
    with open(runs) as f:
        for l in f:
            if path in l or os.path.basename(path) in l: return l.strip()
    return None

def dry_run(packet, ledger, runs, capmap, caps, udir=None):
    check_kill()
    sha = sha256f(packet); name = os.path.basename(packet)
    with open(packet) as f: text = f.read()
    gl, gline = find_go(ledger, sha[:12], name)
    rrow = find_runs(runs, packet)
    job = hashlib.sha256((sha + ":run").encode()).hexdigest()[:16]  # idempotency key
    r = route(text, capmap, caps, udir, job=job)
    ok = bool(gl and rrow and r["ranked"])
    handoff = {"would_send_to": r["ranked"][0]["harness"] if r["ranked"] else None,
               "packet": packet, "packet_sha": sha, "job": job, "go_line": gl,
               "status": "WOULD DISPATCH (dry-run, nothing sent)" if ok else "HOLD"}
    out, hits = redact({"handoff": handoff, "route": r, "runs_row_found": bool(rrow)})
    return ok, out, hits

def main():
    check_kill()  # KILL first, before parsing anything
    ap = argparse.ArgumentParser(); sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("usage-snapshot"); a.add_argument("--usage-dir")
    a = sp.add_parser("capmap"); a.add_argument("key")
    a = sp.add_parser("route-choose"); a.add_argument("packet"); a.add_argument("--usage-dir")
    a = sp.add_parser("dispatch"); a.add_argument("packet"); a.add_argument("--dry-run", action="store_true", required=True)
    a.add_argument("--ledger", default=C.path("go_ledger")); a.add_argument("--runs", default=C.path("runs")); a.add_argument("--usage-dir")
    a = sp.add_parser("adapters")
    a = sp.add_parser("live", help="live caller; offline unless env DISPATCH_LIVE is exactly 1 or <state-dir>/LIVE exists")
    a.add_argument("packet"); a.add_argument("--ledger", default=C.path("go_ledger")); a.add_argument("--runs", default=C.path("runs")); a.add_argument("--usage-dir")
    a.add_argument("--harness", help="tests and operator use only: pin this harness after full checks; refusal exits 7; unset leaves routing unchanged")
    x = ap.parse_args(); check_kill()
    capmap = load(os.path.join(HERE, "capability-map.json")); caps = load(os.path.join(HERE, "caps.json"))
    if x.cmd == "usage-snapshot":
        for h in FALLBACK:
            u, s = usage_for(h, x.usage_dir); print(f"{h:12} U={u:.3f} {s}")
        if all(usage_for(h, x.usage_dir)[1].startswith("unknown") for h in FALLBACK): print(PHASE1_NOTE)
    elif x.cmd == "capmap":
        print(json.dumps(capmap.get(x.key) or {h: v.get(x.key) for h, v in capmap.items()}, indent=1))
    elif x.cmd == "route-choose":
        with open(x.packet) as f: pt = f.read()
        r = route(pt, capmap, caps, x.usage_dir)
        if r["phase1"]: print(PHASE1_NOTE)
        print("PRECEDENCE: hard rules -> score -> fallback (tie/unknown only)")
        print(json.dumps(r, indent=1))
    elif x.cmd == "dispatch":
        ok, out, hits = dry_run(x.packet, x.ledger, x.runs, capmap, caps, x.usage_dir)
        if out["route"]["phase1"]: print(PHASE1_NOTE)
        print(json.dumps(out, indent=1)); print(f"redaction_hits={hits}")
        sys.exit(0 if ok else 2)
    elif x.cmd == "live":
        import live as LV  # lazy: offline commands never import the live path
        rc, out = LV.run(x.packet, x.ledger, x.runs, capmap, caps, x.usage_dir, pin=x.harness)
        print(json.dumps(out, indent=1)); sys.exit(rc)
    elif x.cmd == "adapters":
        print(json.dumps(ADAPTERS, indent=1)); print(f"global kill file: {KILL}")

if __name__ == "__main__": main()
