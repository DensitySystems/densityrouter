#!/usr/bin/env python3
"""dr-front: route, dispatch, and subscribe. Stdlib only.

At-most-once per job_id. Dispatch stores a claim, starts a detached worker, and
returns accepted. Subscribe reads the audit. Phase 3 (users, hidepid, the intake
key, Cloudflare) is not this program.

go_runs_gate defaults to required, which is today's GO and RUNS check. Setting it
to off does not remove the check inside live.py on this tree. That check leaves
when the GO gate itself is removed. There is no intake_daily_max and no
caller-versus-pool rule.
"""
import base64, hashlib, json, os, sqlite3, sys, time
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

AUDIT_FIELDS = (
    "seq", "ts", "prev", "dp", "action", "state", "exit", "job_id", "session_id", "turn",
    "call", "packet_sha256", "router_job", "harness", "result_path", "result_sha256",
    "caller_fpr", "notify", "consumes",
)
ZERO_PREV = "0" * 64
NOTIFY = {"killed", "config_invalid", "hold_audit_broken", "hold_drift", "unknown_outcome",
          "secret_refused", "hold_secret_argv"}
MANIFEST_FILES = (
    "dr_front.py", "dispatcher.py", "live.py", "harness_call.py", "usage_layer.py",
    "usage_writer.py", "drconfig.py", "capability-map.json",
)

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _canon(row):
    return json.dumps({k: row.get(k) for k in AUDIT_FIELDS}, sort_keys=True, separators=(",", ":")).encode()

def _sha(b):
    return hashlib.sha256(b).hexdigest()

def _state_dir():
    import drconfig as C
    return os.environ.get("DISPATCH_STATE") or C.path("state_dir") or os.path.join(HERE, "state")

def _lock_dir():
    return os.environ.get("DR_FRONT_LOCK_DIR") or "/run/densityrouter"

def _etc_dir():
    return os.environ.get("DR_FRONT_ETC") or "/etc/densityrouter"

def _caller():
    return os.environ.get("DR_FRONT_CALLER_FPR") or "local"

def _db_path(state):
    return os.path.join(state, "router.db")

def _connect(state):
    os.makedirs(state, 0o700, exist_ok=True)
    con = sqlite3.connect(_db_path(state), timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
      job_id TEXT PRIMARY KEY, state TEXT, packet_sha256 TEXT, packet_path TEXT,
      session_id TEXT, turn INTEGER, harness TEXT, router_job TEXT, result_path TEXT,
      result_sha256 TEXT, caller_fpr TEXT, consumes INTEGER, ledger TEXT, runs TEXT,
      prompt_path TEXT
    );
    CREATE TABLE IF NOT EXISTS events (
      seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, prev TEXT, dp TEXT, action TEXT,
      state TEXT, exit INTEGER, job_id TEXT, session_id TEXT, turn INTEGER, call TEXT,
      packet_sha256 TEXT, router_job TEXT, harness TEXT, result_path TEXT,
      result_sha256 TEXT, caller_fpr TEXT, notify TEXT, consumes INTEGER, row_hash TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
      session_id TEXT PRIMARY KEY, caller_fpr TEXT, harness TEXT, created REAL,
      idle REAL, last_job TEXT, last_result TEXT
    );
    """)
    try: os.chmod(_db_path(state), 0o600)
    except OSError: pass
    return con

def _public(row):
    d = {}
    keys = row.keys() if hasattr(row, "keys") else ()
    for k in AUDIT_FIELDS:
        d[k] = row[k] if k in keys else None
    if isinstance(d["consumes"], int):
        d["consumes"] = bool(d["consumes"])
    for k, v in list(d.items()):
        if isinstance(v, str) and len(v) > 256:
            raise ValueError("audit field too long")
        if k not in AUDIT_FIELDS:
            raise ValueError("audit field")
    return d

def mirror_journald(line):
    if os.environ.get("DR_FRONT_JOURNAL") == "0":
        return
    import subprocess
    try:
        subprocess.run(["systemd-cat", "-t", "densityrouter-audit"], input=line, text=True, timeout=1, check=False)
    except Exception:
        return

def _audit_lock(state):
    import fcntl
    fd = os.open(os.path.join(state, "audit.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd

def _audit_unlock(fd):
    import fcntl
    try: fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError: pass
    try: os.close(fd)
    except OSError: pass

def verify_chain(con):
    prev = ZERO_PREV
    for row in con.execute("SELECT * FROM events ORDER BY seq"):
        pub = _public(row)
        if pub["prev"] != prev:
            return False
        prev = _sha(_canon(pub))
        if row["row_hash"] and row["row_hash"] != prev:
            return False
    return True

def append_event(con, fields):
    prev = ZERO_PREV
    last = con.execute("SELECT row_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    if last and last["row_hash"]:
        prev = last["row_hash"]
    base = {k: None for k in AUDIT_FIELDS}
    base.update(fields)
    base["prev"] = prev
    base["ts"] = base.get("ts") or _now()
    base["consumes"] = 1 if base.get("consumes") else 0
    base["notify"] = "jonathan" if base.get("state") in NOTIFY else (base.get("notify") or "caller")
    cols = [k for k in AUDIT_FIELDS if k != "seq"]
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "INSERT INTO events (%s) VALUES (%s)" % (",".join(cols), ",".join("?" for _ in cols)),
            [base[k] for k in cols])
        seq = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        base["seq"] = seq
        pub = _public(base)
        digest = _sha(_canon(pub))
        con.execute("UPDATE events SET row_hash=? WHERE seq=?", (digest, seq))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    mirror_journald(json.dumps(pub, sort_keys=True))
    return pub

def scan_proc(root):
    """Pid and argv0 only. The rest of the command line is not returned."""
    hits = []
    if not root or not os.path.isdir(root):
        return hits
    for name in os.listdir(root):
        if not name.isdigit():
            continue
        try:
            data = open(os.path.join(root, name, "cmdline"), "rb").read()
        except OSError:
            continue
        text = data.replace(b"\x00", b" ").decode("utf-8", "replace")
        if "--api-key" in text or "--token" in text or "_KEY=" in text:
            hits.append({"pid": int(name), "argv0": text.split(" ", 1)[0][:64]})
    return hits

def manifest_ok(root=None, manifest=None):
    root = root or HERE
    manifest = manifest or os.path.join(root, "MANIFEST.sha256")
    if not os.path.isfile(manifest):
        return False
    try:
        lines = open(manifest, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    if not lines:
        return False
    for line in lines:
        if not line.strip():
            continue
        try:
            digest, rel = line.split(None, 1)
        except ValueError:
            return False
        rel = rel.strip()
        if rel.startswith("/") or ".." in rel.split("/"):
            return False
        path = os.path.join(root, rel)
        try:
            got = hashlib.sha256(open(path, "rb").read()).hexdigest()
        except OSError:
            return False
        if got != digest:
            return False
    return True

def install_ok(dec):
    if not dec.get("enforce_install"):
        return True
    root = os.path.realpath(HERE)
    if not root.startswith("/opt/densityrouter"):
        return False
    try:
        st = os.stat(os.path.join(HERE, "dr_front.py"))
    except OSError:
        return False
    return st.st_uid == 0 and (st.st_mode & 0o022) == 0

def kill_present():
    import dispatcher as D
    paths = [os.path.join(_etc_dir(), "KILL"), D.KILL]
    for p in paths:
        if p and os.path.exists(p):
            return True
    return False

def write_inbox(raw, digest, state):
    inbox = os.path.join(state, "inbox")
    os.makedirs(inbox, 0o700, exist_ok=True)
    path = os.path.join(inbox, digest + ".md")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow
    try:
        fd = os.open(path, flags, 0o600)
        os.write(fd, raw)
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        got = hashlib.sha256(os.read(fd, len(raw) + 8)).hexdigest()
        os.close(fd)
        if got != digest:
            return None
        return path
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | nofollow)
        try:
            got = hashlib.sha256(os.read(fd, len(raw) + 8)).hexdigest()
        finally:
            os.close(fd)
        if got != digest:
            return None
        return path

def decode_packet(body):
    b64 = body.get("packet_b64")
    expect = body.get("packet_sha256")
    if not isinstance(b64, str) or not isinstance(expect, str) or len(expect) != 64:
        return None, None, "entry"
    try:
        try:
            raw = base64.b64decode(b64, validate=True)
        except TypeError:
            raw = base64.standard_b64decode(b64)
    except Exception:
        return None, None, "entry"
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expect.lower():
        return None, None, "entry"
    return raw, digest, None

def _job_ok(job_id):
    return isinstance(job_id, str) and len(job_id) == 16 and all(c in "0123456789abcdef" for c in job_id)

def _reply(state, exit_code, extra=None):
    out = {"state": state, "exit": exit_code}
    if extra:
        out.update(extra)
    sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
    sys.stdout.flush()
    return exit_code

def _finish(con, state_dir, fields, exit_code, extra=None):
    fd = _audit_lock(state_dir)
    try:
        if not verify_chain(con):
            pub = append_event(con, {"dp": "dp.audit", "action": "stop", "state": "hold_audit_broken",
                                     "exit": 9, "call": fields.get("call"), "consumes": 0, "notify": "jonathan"})
            _reply("hold_audit_broken", 9, {"seq": pub["seq"]})
            return 9
        pub = append_event(con, fields)
    finally:
        _audit_unlock(fd)
    extra = dict(extra or {})
    extra["seq"] = pub["seq"]
    return _reply(fields.get("state"), exit_code, extra)

def _claim_flock(path, blocking):
    import fcntl
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB))
        return fd
    except (BlockingIOError, OSError):
        os.close(fd)
        return None

def _dispatch_lock(blocking, wait_s):
    import fcntl
    os.makedirs(_lock_dir(), 0o700, exist_ok=True)
    path = os.path.join(_lock_dir(), "dispatch.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.time() + max(0, wait_s)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            if blocking and time.time() < deadline:
                time.sleep(0.05)
                continue
            os.close(fd)
            return None

def spawn_worker(job_id):
    cmd = [sys.executable, os.path.abspath(__file__), "worker", job_id]
    runner = os.environ.get("DR_FRONT_SYSTEMD_RUN")
    if runner:
        argv = [runner, "--unit", "dr-%s" % job_id, "--"] + cmd
    else:
        argv = ["systemd-run", "--user", "--collect", "--unit", "dr-%s" % job_id, "--"] + cmd
    import subprocess
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    return p.returncode == 0

def _result_file(state, job_id):
    d = os.path.join(state, "jobs", job_id)
    os.makedirs(d, 0o700, exist_ok=True)
    return os.path.join(d, "result.json"), os.path.join(d, "claim"), os.path.join(d, "ready")

def _write_result(path, obj):
    raw = json.dumps(obj, sort_keys=True).encode()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.write(fd, raw)
    os.fsync(fd)
    os.close(fd)
    os.replace(tmp, path)
    dirfd = os.open(os.path.dirname(path), os.O_RDONLY)
    try: os.fsync(dirfd)
    finally: os.close(dirfd)
    return hashlib.sha256(raw).hexdigest()

def worker_main(job_id):
    os.umask(0o077)
    state = _state_dir()
    con = _connect(state)
    job = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if job is None:
        return 2
    result_path, claim_path, ready_path = _result_file(state, job_id)
    claim_fd = _claim_flock(claim_path, True)
    lock_fd = _dispatch_lock(True, 0)
    if lock_fd is None:
        import drconfig as C
        wait = 5
        try: wait = int(C.decision()["lock_wait_s"])
        except Exception: pass
        lock_fd = _dispatch_lock(True, wait)
    if lock_fd is None:
        con.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        fd = _audit_lock(state)
        try:
            append_event(con, {"dp": "dp.lock", "action": "stop", "state": "lock_held", "exit": 2,
                               "job_id": job_id, "call": "dispatch", "consumes": 0, "notify": "caller"})
        finally:
            _audit_unlock(fd)
        if claim_fd is not None:
            os.close(claim_fd)
        return 2
    open(ready_path, "w").close()
    delay = float(os.environ.get("DR_FRONT_FAKE_DELAY") or "0")
    if delay:
        time.sleep(delay)
    prompt_path = job["prompt_path"] or job["packet_path"]
    if os.environ.get("DR_FRONT_FAKE_LIVE") == "1":
        text = ""
        try: text = open(prompt_path, encoding="utf-8").read()
        except OSError: pass
        result = {"status": "done", "harness": "cursor", "stdout": "ok", "prompt_prefix": text[:80]}
        rc = 0
    else:
        import subprocess
        live = os.path.join(HERE, "live.py")
        argv = [sys.executable, live, prompt_path]
        if job["ledger"]:
            argv += ["--ledger", job["ledger"]]
        if job["runs"]:
            argv += ["--runs", job["runs"]]
        if job["turn"] and int(job["turn"]) > 1 and job["harness"]:
            argv += ["--harness", job["harness"]]
        env = os.environ.copy()
        env["DENSITYROUTER_JOB_ID"] = job_id
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=None, env=env)
            err = p.stderr or ""
            import dispatcher as D
            if D.exact_secret_hit(err) or D.exact_secret_hit(p.stdout or ""):
                result = {"status": "secret_refused", "harness": None, "stdout": "", "stderr": ""}
                rc = 6
            else:
                try:
                    result = json.loads(p.stdout)
                except Exception:
                    q = os.path.join(state, "jobs", job_id, "unreadable.bin")
                    open(q, "wb").write((p.stdout or "").encode("utf-8", "replace"))
                    os.chmod(q, 0o600)
                    result = {"status": "reply_unreadable", "harness": None, "stdout": ""}
                    rc = 6
                else:
                    rc = p.returncode
                    if isinstance(result, dict) and result.get("call"):
                        st = (result.get("call") or {}).get("status")
                        if st:
                            result = {"status": st, "harness": (result.get("call") or {}).get("harness"),
                                      "stdout": (result.get("call") or {}).get("stdout") or "", "router": result}
        except Exception as ex:
            result = {"status": "internal_error", "error_class": type(ex).__name__, "stdout": ""}
            rc = 6
    import dispatcher as D
    if isinstance(result, dict) and D.exact_secret_hit(result.get("stdout") or ""):
        result = {"status": "secret_refused", "harness": result.get("harness"), "stdout": ""}
        rc = 6
    digest = _write_result(result_path, result)
    status = result.get("status") if isinstance(result, dict) else "reply_unreadable"
    harness = result.get("harness") if isinstance(result, dict) else None
    consumes = 0 if status in ("lock_held", "in_flight") else 1
    state_name = status or "reply_unreadable"
    if status == "done":
        state_name = "done"
    elif status in ("failed_retryable", "timed_out", "failed_hold:usage", "failed_hold:not_installed", "NOT_DISPATCHED"):
        state_name = "retry_exhausted"
    con.execute(
        "UPDATE jobs SET state=?, result_path=?, result_sha256=?, harness=COALESCE(?, harness), consumes=? WHERE job_id=?",
        (state_name, result_path, digest, harness, consumes, job_id))
    if job["session_id"] and harness and status == "done":
        con.execute("UPDATE sessions SET harness=?, idle=?, last_job=?, last_result=? WHERE session_id=?",
                    (harness, time.time(), job_id, result_path, job["session_id"]))
    fd = _audit_lock(state)
    try:
        append_event(con, {
            "dp": "dp.worker", "action": "finish", "state": state_name, "exit": rc,
            "job_id": job_id, "session_id": job["session_id"], "turn": job["turn"], "call": "dispatch",
            "packet_sha256": job["packet_sha256"], "router_job": job["router_job"], "harness": harness,
            "result_path": result_path, "result_sha256": digest, "caller_fpr": job["caller_fpr"],
            "consumes": consumes, "notify": "jonathan" if state_name in NOTIFY else "caller"})
    finally:
        _audit_unlock(fd)
    if lock_fd is not None:
        os.close(lock_fd)
    if claim_fd is not None:
        os.close(claim_fd)
    return 0

def _terminal(con, job_id):
    row = con.execute(
        "SELECT * FROM events WHERE job_id=? AND consumes=1 ORDER BY seq DESC LIMIT 1", (job_id,)).fetchone()
    return row

def _session_live(con, session_id, fpr):
    row = con.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None or row["caller_fpr"] != fpr:
        return None
    if time.time() > row["created"] + 4 * 3600:
        return None
    if time.time() > row["idle"] + 30 * 60:
        return None
    return row

def handle(body, proc_root=None):
    os.umask(0o077)
    import drconfig as C
    import dispatcher as D
    call = body.get("call")
    if os.environ.get("SSH_ORIGINAL_COMMAND"):
        return _reply("hold_entry", 2, {"ssh_original_len": len(os.environ.get("SSH_ORIGINAL_COMMAND") or "")})
    if kill_present():
        state = _state_dir()
        try:
            con = _connect(state)
            return _finish(con, state, {"dp": "dp.kill", "action": "stop", "state": "killed", "exit": 3,
                                        "call": call, "consumes": 0, "notify": "jonathan"}, 3)
        except Exception:
            sys.stderr.write("killed\n")
            return 3
    try:
        dec = C.decision()
        C.routing_slot()
    except C.ConfigInvalid:
        return _reply("config_invalid", 9)
    if not install_ok(dec):
        return _reply("config_invalid", 9)
    if not manifest_ok():
        return _reply("hold_drift", 9)
    proc_root = proc_root if proc_root is not None else os.environ.get("DR_FRONT_PROC", "/proc")
    hits = scan_proc(proc_root)
    if hits:
        return _reply("hold_secret_argv", 9, {"pid": hits[0]["pid"], "argv0": hits[0]["argv0"]})
    state = _state_dir()
    con = _connect(state)
    fd = _audit_lock(state)
    try:
        ok = verify_chain(con)
    finally:
        _audit_unlock(fd)
    if not ok:
        return _finish(con, state, {"dp": "dp.audit", "action": "stop", "state": "hold_audit_broken",
                                    "exit": 9, "call": call, "consumes": 0}, 9)
    if call == "doctor":
        return _reply("doctor", 0, {"manifest": True, "routing_empty": True,
                                    "backends_verified": bool(dec["backends_verified"]),
                                    "kill": False, "proc_hits": len(hits)})
    if call not in ("route", "dispatch", "subscribe"):
        return _reply("hold_entry", 2)
    job_id = body.get("job_id")
    if call != "route" and not _job_ok(job_id):
        return _reply("hold_entry", 2)
    if call == "route" and job_id is not None and not _job_ok(job_id):
        return _reply("hold_entry", 2)
    if call == "subscribe":
        return _subscribe(con, state, body, dec)
    if "packet_path" in body:
        return _reply("hold_entry", 2)
    raw, digest, err = decode_packet(body)
    if err:
        return _reply("hold_entry", 2)
    path = write_inbox(raw, digest, state)
    if not path:
        return _reply("hold_entry", 2)
    text = raw.decode("utf-8", "surrogateescape")
    if call == "dispatch" and D.redact_text(text)[1] > 0:
        return _consumed_stop(con, state, body, digest, "dp.secret", "redacted", 8)
    if call == "route":
        return _route(con, state, body, text, digest, job_id)
    return _dispatch(con, state, body, dec, path, text, digest, job_id)

def _consumed_stop(con, state, body, digest, dp, status, exit_code):
    job_id = body.get("job_id") or "0" * 16
    if not _job_ok(job_id):
        return _reply("hold_entry", 2)
    try:
        con.execute("INSERT INTO jobs (job_id, state, packet_sha256, consumes, caller_fpr) VALUES (?,?,?,?,?)",
                    (job_id, status, digest, 1, _caller()))
    except sqlite3.IntegrityError:
        row = _terminal(con, job_id)
        if row:
            return _reply(row["state"], row["exit"] if row["exit"] is not None else 0, {"job_id": job_id})
    return _finish(con, state, {"dp": dp, "action": "stop", "state": status, "exit": exit_code,
                                "job_id": job_id, "call": "dispatch", "packet_sha256": digest,
                                "caller_fpr": _caller(), "consumes": 1}, exit_code, {"job_id": job_id})

def _route(con, state, body, text, digest, job_id):
    import dispatcher as D
    capmap = D.load(os.path.join(HERE, "capability-map.json"))
    caps = D.load(os.path.join(HERE, "caps.json"))
    try:
        ranked = D.route(text, capmap, caps)
    except Exception as ex:
        return _reply("internal_error", 6, {"error_class": type(ex).__name__})
    choice = ranked["ranked"][0]["harness"] if ranked.get("ranked") else None
    extra = {"would_send_to": choice, "reasons": {
        "decided_by": ranked.get("decided_by"), "task_type": ranked.get("task_type"),
        "ranked": ranked.get("ranked"), "skipped": ranked.get("skipped"),
        "health_gap": ranked.get("health_gap"), "basis": ranked.get("basis")}}
    if job_id:
        done = _terminal(con, job_id)
        if done:
            extra["dispatched_to"] = done["harness"]
    state_name = "advisory" if choice else "hold_unranked"
    code = 0 if choice else 2
    if job_id:
        return _finish(con, state, {"dp": "dp.route", "action": "route", "state": state_name, "exit": code,
                                    "job_id": job_id, "call": "route", "packet_sha256": digest,
                                    "caller_fpr": _caller(), "consumes": 0, "notify": "caller"}, code, extra)
    sys.stdout.write(json.dumps({"state": state_name, "exit": code, **extra}, sort_keys=True) + "\n")
    return code

def _dispatch(con, state, body, dec, path, text, digest, job_id):
    import drconfig as C
    fpr = _caller()
    intent = body.get("intent") or "start"
    if intent not in ("start", "close"):
        return _reply("hold_entry", 2)
    existing = _terminal(con, job_id)
    if existing and intent == "start":
        return _reply(existing["state"], 0 if existing["state"] in ("done", "accepted") else (existing["exit"] or 0),
                      {"job_id": job_id, "session_id": existing["session_id"], "idempotent": True})
    if intent == "close":
        sess = _session_live(con, body.get("session_id"), fpr)
        if sess is None:
            return _reply("bad_session", 2)
        con.execute("UPDATE sessions SET idle=? WHERE session_id=?", (0, sess["session_id"]))
        return _finish(con, state, {"dp": "dp.close", "action": "close", "state": "session_closed", "exit": 0,
                                    "job_id": job_id, "session_id": sess["session_id"], "call": "dispatch",
                                    "caller_fpr": fpr, "consumes": 1}, 0, {"job_id": job_id, "session_id": sess["session_id"]})
    row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is not None:
        _rp, claim_path, _ready = _result_file(state, job_id)
        held = _claim_flock(claim_path, False)
        if held is None and row["consumes"] != 1:
            return _reply("in_flight", 0, {"job_id": job_id, "session_id": row["session_id"]})
        if held is not None:
            os.close(held)
            if row["result_sha256"] and row["result_path"] and os.path.isfile(row["result_path"]):
                return _finish(con, state, {"dp": "dp.sweep", "action": "promote", "state": row["state"] or "done",
                                            "exit": 0, "job_id": job_id, "session_id": row["session_id"],
                                            "call": "dispatch", "result_path": row["result_path"],
                                            "result_sha256": row["result_sha256"], "consumes": 1,
                                            "caller_fpr": fpr}, 0, {"job_id": job_id})
            return _finish(con, state, {"dp": "dp.sweep", "action": "unknown", "state": "unknown_outcome",
                                        "exit": 6, "job_id": job_id, "call": "dispatch", "consumes": 1,
                                        "caller_fpr": fpr, "notify": "jonathan"}, 6, {"job_id": job_id})
    if not dec["backends_verified"]:
        return _reply("hold_unverified", 2, {"job_id": job_id})
    if os.path.exists(os.path.join(_etc_dir(), "V3")):
        return _reply("hold_offline", 2, {"job_id": job_id})
    import live as LV
    if not LV.live_enabled():
        return _reply("hold_offline", 2, {"job_id": job_id})
    ledger = C.path("go_ledger")
    runs = C.path("runs")
    if dec["go_runs_gate"] == "required":
        try:
            gl, _line = __import__("dispatcher").find_go(ledger, digest[:12], os.path.basename(path))
            row_runs = __import__("dispatcher").find_runs(runs, path)
        except OSError:
            gl, row_runs = None, None
        if not gl or not row_runs:
            return _reply("hold_go_runs", 2, {"job_id": job_id})
    turn = body.get("turn") or 1
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 1:
        return _reply("hold_entry", 2)
    session_id = body.get("session_id")
    harness = None
    if session_id:
        sess = _session_live(con, session_id, fpr)
        if sess is None:
            return _reply("bad_session", 2, {"job_id": job_id})
        if turn < 2:
            return _reply("hold_entry", 2)
        harness = sess["harness"]
        prior = sess["last_result"] or ""
        prompt = "Prior result: %s\n%s" % (prior, text)
    else:
        import secrets
        session_id = secrets.token_hex(16)
        prompt = text
        now = time.time()
        con.execute("INSERT INTO sessions (session_id, caller_fpr, harness, created, idle, last_job, last_result) VALUES (?,?,?,?,?,?,?)",
                    (session_id, fpr, None, now, now, job_id, None))
    prompt_path = os.path.join(state, "jobs", job_id, "prompt.md")
    os.makedirs(os.path.dirname(prompt_path), 0o700, exist_ok=True)
    pfd = os.open(prompt_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.write(pfd, prompt.encode("utf-8", "surrogateescape"))
    os.close(pfd)
    router_job = hashlib.sha256((digest + ":run").encode()).hexdigest()[:16]
    try:
        con.execute(
            "INSERT INTO jobs (job_id, state, packet_sha256, packet_path, session_id, turn, harness, router_job, caller_fpr, consumes, ledger, runs, prompt_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "accepted", digest, path, session_id, turn, harness, router_job, fpr, 0, ledger, runs, prompt_path))
    except sqlite3.IntegrityError:
        return _reply("in_flight", 0, {"job_id": job_id, "session_id": session_id})
    open(os.path.join(state, "jobs", job_id, "id"), "w").write(job_id)
    if not spawn_worker(job_id):
        con.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        return _reply("worker_not_started", 2, {"job_id": job_id})
    ready = os.path.join(state, "jobs", job_id, "ready")
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(ready):
        time.sleep(0.02)
    if not os.path.exists(ready):
        return _reply("worker_not_started", 2, {"job_id": job_id, "session_id": session_id})
    return _finish(con, state, {"dp": "dp.accept", "action": "accept", "state": "accepted", "exit": 0,
                                "job_id": job_id, "session_id": session_id, "turn": turn, "call": "dispatch",
                                "packet_sha256": digest, "router_job": router_job, "caller_fpr": fpr,
                                "consumes": 0, "notify": "caller"}, 0,
                   {"job_id": job_id, "session_id": session_id})

def _subscribe(con, state, body, dec):
    job_id = body.get("job_id")
    try:
        after = int(body.get("after_sequence") or 0)
    except (TypeError, ValueError):
        return _reply("hold_entry", 2)
    until = body.get("until") or "terminal"
    if until not in ("terminal", "session_close"):
        return _reply("hold_entry", 2)
    wait = body.get("max_wait_s")
    if wait is None:
        wait = dec["max_wait_s"]
    try:
        wait = float(wait)
    except (TypeError, ValueError):
        return _reply("hold_entry", 2)
    last = con.execute("SELECT MAX(seq) AS s FROM events").fetchone()["s"] or 0
    if after > last:
        return _reply("bad_cursor", 2, {"last_seq": last})
    deadline = time.time() + max(0, wait)
    while True:
        rows = list(con.execute("SELECT * FROM events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after)))
        term = _terminal(con, job_id)
        if term or (until == "session_close" and any(r["state"] == "session_closed" for r in rows)):
            break
        if time.time() >= deadline:
            return _reply("subscribe_timeout", 6, {"job_id": job_id})
        time.sleep(0.05)
    out_rows = []
    for r in con.execute("SELECT * FROM events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after)):
        out_rows.append(_public(r))
    result = None
    status = term["state"] if term else "session_closed"
    if term and term["result_path"] and term["state"] == "done":
        try:
            raw = open(term["result_path"], "rb").read()
        except OSError:
            raw = b""
        if hashlib.sha256(raw).hexdigest() == term["result_sha256"]:
            try:
                result = json.loads(raw.decode())
            except Exception:
                result = None
    sys.stdout.write(json.dumps({"state": status, "exit": 0 if status == "done" else 6,
                                 "events": out_rows, "result": result}, sort_keys=True) + "\n")
    return 0 if status == "done" else 6

def main(argv=None):
    os.umask(0o077)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "worker":
        if len(argv) != 2:
            return 2
        return worker_main(argv[1])
    try:
        raw = sys.stdin.read()
        body = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.stderr.write("ValueError\n")
        return 6
    if not isinstance(body, dict) or body.get("schema") not in (1, None):
        return _reply("hold_entry", 2)
    try:
        return handle(body)
    except SystemExit:
        raise
    except Exception as ex:
        sys.stderr.write(type(ex).__name__ + "\n")
        return 6

if __name__ == "__main__":
    sys.exit(main())
