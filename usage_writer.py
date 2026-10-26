#!/usr/bin/env python3
"""Per-harness usage counter and snapshot writer. stdlib only.

Declarations ship unset. A snapshot is written only when allowance, window_len_h,
and reset_at are all set. Otherwise the result is state config-gap and <harness>.json
is left unwritten so dispatcher.usage_for keeps its current missing-file behavior.
Counters live beside snapshots as <harness>.counter.json. Nothing else is read or written.
"""
import argparse, json, os, sys, tempfile
from datetime import datetime, timedelta

sys.dont_write_bytecode = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drconfig as C
HARNESSES = tuple(C.routed())  # routed harnesses only (config route=true)
DECL_FIELDS = ("allowance", "window_len_h", "reset_at")
DECLARATIONS = {h: {"allowance": None, "window_len_h": None, "reset_at": None} for h in HARNESSES}

DEFAULT_USAGE_DIR = C.path("usage_dir")
USAGE_DIR_ENV = "DISPATCH_USAGE_DIR"
MIN_ELAPSED_H = 0.1


def resolve_usage_dir(udir=None):
    """Function argument wins, then DISPATCH_USAGE_DIR, then the default dir."""
    if udir is not None:
        return udir
    env = os.environ.get(USAGE_DIR_ENV)
    if env:
        return env
    return DEFAULT_USAGE_DIR


def _require_harness(h):
    if h not in HARNESSES:
        raise ValueError("unknown harness %r; expected one of %s" % (h, ", ".join(HARNESSES)))
    return h


def _now():
    return datetime.now().astimezone().replace(microsecond=0)


def coerce_dt(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = datetime.fromtimestamp(value).astimezone()
    else:
        raise TypeError("time must be a datetime, ISO8601 string, or epoch seconds")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt.replace(microsecond=0)


_coerce_dt = coerce_dt


def _iso(dt):
    return _coerce_dt(dt).isoformat(timespec="seconds")


def is_unset(value):
    return value is None or value == ""


_is_unset = is_unset


def _load_decl(decl):
    if decl is None:
        return DECLARATIONS
    if isinstance(decl, str):
        with open(decl) as f:
            return json.load(f)
    if isinstance(decl, dict):
        return decl
    raise TypeError("decl must be a path, a dict, or None")


def fields_for(h, decl):
    raw = _load_decl(decl)
    if h in raw and isinstance(raw[h], dict):
        src = raw[h]
    elif any(k in raw for k in DECL_FIELDS):
        src = raw
    else:
        src = {}
    return {k: src.get(k) for k in DECL_FIELDS}


_fields_for = fields_for


def missing(fields):
    return [k for k in DECL_FIELDS if is_unset(fields[k])]


_missing = missing


def read_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


_read_json = read_json


def _atomic_write(path, obj):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fd = None
            json.dump(obj, fh, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def counter_path(udir, h):
    return os.path.join(udir, h + ".counter.json")


_counter_path = counter_path


def _snapshot_path(udir, h):
    return os.path.join(udir, h + ".json")


def _advance(reset_at, step, now):
    """Move reset_at forward by whole windows until it is strictly after now."""
    if now < reset_at:
        return reset_at
    step_s = step.total_seconds()
    if step_s <= 0:
        raise ValueError("window_len_h must be positive")
    steps = int((now - reset_at).total_seconds() // step_s) + 1
    return reset_at + step * steps


def _project(fields, now, counter):
    """Window that contains now, and the count that still applies inside it.

    A count from an earlier window does not carry. The stored counter is not
    modified here; record_call persists the projection it is about to increment.
    """
    step = timedelta(hours=float(fields["window_len_h"]))
    if step.total_seconds() <= 0:
        raise ValueError("window_len_h must be positive")
    if counter is None:
        reset_at = _advance(_coerce_dt(fields["reset_at"]), step, now)
        return reset_at - step, reset_at, 0
    reset_at = _coerce_dt(counter["reset_at"])
    start = _coerce_dt(counter["window_start"])
    count = int(counter["count"])
    if now >= reset_at:
        reset_at = _advance(reset_at, step, now)
        start = reset_at - step
        count = 0
    return start, reset_at, count


def _as_n(n):
    if isinstance(n, bool) or isinstance(n, float) and not n.is_integer() or not isinstance(n, (int, float)):
        raise ValueError("n must be a non-negative integer")
    n = int(n)
    if n < 0:
        raise ValueError("n must be a non-negative integer")
    return n


def record_call(h, n=1, now=None, decl=None, udir=None):
    """Increment the harness counter. Past reset_at, the old count is dropped."""
    h = _require_harness(h)
    n = _as_n(n)
    now = _now() if now is None else _coerce_dt(now)
    fields = _fields_for(h, decl)
    if _is_unset(fields["window_len_h"]) or _is_unset(fields["reset_at"]):
        raise ValueError("declaration missing window_len_h/reset_at for %s" % h)
    udir = resolve_usage_dir(udir)
    counter = _read_json(_counter_path(udir, h))
    start, reset_at, count = _project(fields, now, counter)
    obj = {
        "harness": h,
        "window_start": _iso(start),
        "reset_at": _iso(reset_at),
        "count": count + n,
    }
    _atomic_write(_counter_path(udir, h), obj)
    return obj


def write_snapshot(h, now=None, decl=None, udir=None):
    """Write <harness>.json, or report config-gap and write nothing."""
    h = _require_harness(h)
    now = _now() if now is None else _coerce_dt(now)
    fields = _fields_for(h, decl)
    missing = _missing(fields)
    read_at = _iso(now)
    if missing:
        # Do not write <harness>.json. No remaining value is invented here.
        return {"harness": h, "state": "config-gap", "missing": missing, "read_at": read_at}
    udir = resolve_usage_dir(udir)
    counter = _read_json(_counter_path(udir, h))
    start, reset_at, count = _project(fields, now, counter)
    allowance = fields["allowance"]
    if isinstance(allowance, bool) or not isinstance(allowance, (int, float)) or float(allowance) <= 0:
        raise ValueError("allowance must be a positive number")
    allowance = float(allowance)
    elapsed_h = (now - start).total_seconds() / 3600.0
    if elapsed_h < MIN_ELAPSED_H:
        elapsed_h = MIN_ELAPSED_H
    used_pct = count / allowance * 100.0
    snap = {
        "harness": h,
        "remaining_pct": round(max(0.0, allowance - count) / allowance * 100.0, 1),
        "window_len_h": fields["window_len_h"],
        "reset_at": _iso(reset_at),
        "burn_pct_per_h": used_pct / elapsed_h,
        "read_at": read_at,
        "state": "counter",
        "source": "own-counter",
    }
    _atomic_write(_snapshot_path(udir, h), snap)
    return snap


def _num_pct(rec):
    if not isinstance(rec, dict):
        return None
    v = rec.get("remaining_pct")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _delta_state(before, after):
    """measured when both percents are numbers; else missing wins over config-gap."""
    if _num_pct(before) is not None and _num_pct(after) is not None:
        return "measured"

    def kind(rec):
        if not isinstance(rec, dict):
            return "missing"
        state = rec.get("state")
        if state == "missing":
            return "missing"
        if state == "config-gap":
            return "config-gap"
        if _num_pct(rec) is None:
            return "missing"
        return "snapshot"

    kinds = (kind(before), kind(after))
    if "missing" in kinds:
        return "missing"
    if "config-gap" in kinds:
        return "config-gap"
    return "missing"


def read_usage(h, udir=None, now=None):
    """Read one harness snapshot. Does not write.

    A readable <udir>/<h>.json object is state "snapshot". Declarations unset
    and no snapshot is state "config-gap" with remaining_pct None (never a
    number). Any other absence is state "missing", remaining_pct None.
    """
    now_dt = _now() if now is None else _coerce_dt(now)
    read_at = _iso(now_dt)
    udir = resolve_usage_dir(udir)
    snap = None
    path = _snapshot_path(udir, h)
    if os.path.isfile(path):
        try:
            loaded = _read_json(path)
        except (OSError, ValueError, TypeError):
            loaded = None
        if isinstance(loaded, dict):
            snap = loaded
    if isinstance(snap, dict):
        return {"harness": h, "state": "snapshot", "remaining_pct": _num_pct(snap), "read_at": read_at}
    if _missing(_fields_for(h, None)):
        return {"harness": h, "state": "config-gap", "remaining_pct": None, "read_at": read_at}
    return {"harness": h, "state": "missing", "remaining_pct": None, "read_at": read_at}


def log_usage_delta(job, h, before, after, udir=None, t_start=None, t_end=None):
    """Append one JSON object to <udir>/usage-log.jsonl. Returns the object."""
    udir = resolve_usage_dir(udir)
    bp, ap = _num_pct(before), _num_pct(after)
    rec = {
        "job": job,
        "harness": h,
        "t_start": t_start,
        "t_end": t_end,
        "before": before,
        "after": after,
        "delta_pct": None if bp is None or ap is None else bp - ap,
        "delta_state": _delta_state(before, after),
    }
    os.makedirs(udir, exist_ok=True)
    with open(os.path.join(udir, "usage-log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return rec


def main(argv=None):
    p = argparse.ArgumentParser(prog="usage_writer.py")
    p.add_argument("--usage-dir", default=None)
    p.add_argument("--decl", default=None)
    p.add_argument("action", choices=("record", "snapshot"))
    p.add_argument("harness")
    args = p.parse_args(argv)
    try:
        if args.action == "record":
            if args.harness == "all":
                raise ValueError("record takes one harness, not all")
            obj = record_call(args.harness, decl=args.decl, udir=args.usage_dir)
        elif args.harness == "all":
            obj = [write_snapshot(h, decl=args.decl, udir=args.usage_dir) for h in HARNESSES]
        else:
            obj = write_snapshot(args.harness, decl=args.decl, udir=args.usage_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(obj, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
