"""Router build 8 usage metering. stdlib unittest.
Temp DISPATCH_STATE and temp usage dirs only. Fake call_fn and reader_fn.
DISPATCH_LIVE is only an env dict passed to live.run. No real state/LIVE.
Run: python3 tests/test_usage_metering.py
"""
import json, os, re, shutil, subprocess, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, harness_call as HC, live as LV, usage_writer as UW

ISO_OFF = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")
TEXT = (
    "# PACKET: meter\n"
    "\n"
    "## Objective\n"
    "Fix a script in the scratch dir.\n"
)


class UsageMetering(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        self.udir = os.path.join(self.tmp, "usage")
        os.makedirs(self.state)
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL", "DISPATCH_LIVE")}
        os.environ["DISPATCH_STATE"] = self.state
        os.environ.pop("DISPATCH_LIVE", None)
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL")
        os.environ["DISPATCH_KILL"] = D.KILL
        self._sub = subprocess.run
        self._hc = HC.subprocess.run

        def boom(*a, **k):
            raise AssertionError("subprocess.run")

        subprocess.run = boom
        HC.subprocess.run = boom
        self.pk = os.path.join(self.tmp, "PACKET-METER-r1.md")
        with open(self.pk, "w", encoding="utf-8") as f:
            f.write(TEXT)
        sha12 = D.sha256f(self.pk)[:12]
        self.ledger = os.path.join(self.tmp, "ledger.md")
        self.runs = os.path.join(self.tmp, "RUNS.tsv")
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write("| 1 | GO | PACKET-METER-r1.md %s |\n" % sha12)
        with open(self.runs, "w", encoding="utf-8") as f:
            f.write("x\t%s\n" % self.pk)
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = D.load(os.path.join(ROOT, "caps.json"))
        self.default_log = os.path.join(UW.DEFAULT_USAGE_DIR, "usage-log.jsonl")
        self._default_existed = os.path.exists(self.default_log)

    def tearDown(self):
        subprocess.run = self._sub
        HC.subprocess.run = self._hc
        D.KILL = self._kill
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        self.assertEqual(os.path.exists(self.default_log), self._default_existed)

    def _go(self, call_fn, reader, pass_udir=True):
        env = {"DISPATCH_LIVE": "1", "DISPATCH_STATE": self.state}
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        kw = {"env": env, "call_fn": call_fn,
              "reader_fn": reader}
        if pass_udir:
            kw["udir"] = self.udir
        return LV.run(self.pk, self.ledger, self.runs, self.capmap, self.caps, **kw)

    def _lines(self, path):
        with open(path, encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]

    @staticmethod
    def _rd(h, rem=None, state="read"):
        if rem is None:
            return {"harness": h, "parse_ok": False, "state": state, "pools": {}, "primary": None}
        prim = "weekly" if h == "grok-build" else ("included" if h == "cursor" else "gemini_model_x.weekly")
        return {"harness": h, "parse_ok": True, "state": "read", "primary": prim,
                "pools": {prim: {"used_pct": 100 - rem, "remaining_pct": rem}}}

    def _ledger(self):
        return [json.loads(x) for x in self._lines(os.path.join(self.state, "usage-ledger.jsonl"))]

    def test_measured_delta_and_dispatch_log(self):
        n = {"i": 0}

        def reader(h):
            n["i"] += 1
            return self._rd(h, 80 if n["i"] == 1 else 70)

        def call_fn(h, job=None, packet=None, **k):
            return {"harness": h, "status": "done", "rc": 0, "stdout": "secret-ish reply"}

        rc, out = self._go(call_fn, reader)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(n["i"], 2)
        reading = out["usage"][0]; h = reading["harness"]; job = out["handoff"]["job"]
        rows = self._ledger()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["request_id"], row["harness"], row["parse_ok"]), (job, h, True))
        self.assertEqual(row["delta"][row["pre"]["primary"]]["used_pct"], 10)
        self.assertEqual(row["reply"]["len"], len("secret-ish reply"))
        self.assertNotIn("secret-ish", json.dumps(row))
        dline = self._lines(os.path.join(self.state, "dispatch.log"))[0]
        for t in ("job=%s" % job, "harness=%s" % h, "before=80", "after=70",
                  "start=%s" % D._log_iso(reading["t_start"]), "end=%s" % D._log_iso(reading["t_end"])):
            self.assertIn(t, dline)
        self.assertRegex(dline, r"start=" + ISO_OFF.pattern)
        self.assertFalse(os.path.exists(os.path.join(self.state, "LIVE")))
        self.assertFalse(os.path.exists(os.path.join(self.udir, "usage-log.jsonl")))
        ev = out["lease_evidence"][0]                                   # lease released, KILL shown
        self.assertEqual((ev["kept"], ev["release"][0], ev["lease_files_after"], ev["kill_present"]), (False, True, [], False))
        self.assertTrue(ev["release"][1].startswith("RELEASED"))  # old item-5 log no longer written

    def test_unreadable_reading_flagged_not_zero(self):
        rc, out = self._go(lambda h, **k: {"harness": h, "status": "done"}, lambda h: self._rd(h, None, "parse_failed"))
        self.assertEqual(rc, 0, out["handoff"]["status"])
        row = self._ledger()[0]
        self.assertFalse(row["parse_ok"]); self.assertEqual(row["delta"], "unknown"); self.assertTrue(row["flagged"])
        dline = self._lines(os.path.join(self.state, "dispatch.log"))[0]
        self.assertNotRegex(dline, r"before=\d")

    def test_injected_call_without_reader_never_reads_cli(self):
        import usage_layer as UL
        orig = UL.read_usage
        def boom(*a, **k):
            raise AssertionError("real /usage read")
        UL.read_usage = boom
        try:
            rc, out = self._go(lambda h, **k: {"harness": h, "status": "done"}, None)
        finally:
            UL.read_usage = orig
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(out["usage"][0]["before"]["state"], "not_read")
        self.assertEqual(self._ledger()[0]["delta"], "unknown")

    def test_post_reader_throws_result_still_written(self):
        n = {"i": 0}
        def reader(h):
            n["i"] += 1
            if n["i"] == 2: raise RuntimeError("usage screen gone")
            return self._rd(h, 80)
        rc, out = self._go(lambda h, **k: {"harness": h, "status": "done", "rc": 0, "stdout": "r"}, reader)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        row = self._ledger()[0]
        self.assertFalse(row["parse_ok"]); self.assertEqual(row["post"]["state"], "read_failed")
        self.assertEqual(out["attempts"][0]["status"], "done")

    def test_reads_async_never_hold_call_and_overhead_logged(self):  # 
        import time as T
        seen = {}
        def reader(h):
            T.sleep(0.6); return self._rd(h, 80)
        def call_fn(h, **k):
            seen["t"] = T.time(); return {"harness": h, "status": "done", "rc": 0, "stdout": "r"}
        t0 = T.time()
        rc, out = self._go(call_fn, reader)
        self.assertLess(seen["t"] - t0, 0.5)     # the call did not wait for the 0.6 s pre read
        ov = self._ledger()[0]["usage_read_overhead_s"]
        self.assertGreaterEqual(ov["pre"], 0.5); self.assertGreaterEqual(ov["post"], 0.5); self.assertFalse(ov["blocking"])

    def test_read_timeout_logged_read_failed(self):
        import time as T
        old = LV._async_read.READ_WAIT_S; LV._async_read.READ_WAIT_S = 0.2
        try:
            rc, out = self._go(lambda h, **k: {"harness": h, "status": "done"}, lambda h: (T.sleep(1.5), self._rd(h, 80))[1])
        finally:
            LV._async_read.READ_WAIT_S = old
        self.assertEqual(rc, 0)
        row = self._ledger()[0]
        self.assertEqual((row["pre"]["state"], row["post"]["state"], row["parse_ok"]), ("read_failed", "read_failed", False))

    def test_reap_runs_before_post_read(self):
        import time as T
        ev = []
        def reader(h):
            ev.append(("read", T.time())); return self._rd(h, 80)
        def call_fn(h, **k):
            ev.append(("call", T.time())); return {"harness": h, "status": "done", "rc": 0, "stdout": "r"}
        def reap(cwd, since):
            ev.append(("reap", T.time())); return [4242]
        env = {"DISPATCH_LIVE": "1", "DISPATCH_STATE": self.state}
        rc, out = LV.run(self.pk, self.ledger, self.runs, self.capmap, self.caps, udir=self.udir, env=env,
                         call_fn=call_fn, reader_fn=reader, reap_fn=reap)
        self.assertEqual(rc, 0)
        kinds = [k for k, _ in sorted(ev, key=lambda x: x[1])]
        i_call, i_reap = kinds.index("call"), kinds.index("reap")
        self.assertLess(i_call, i_reap)
        self.assertEqual(kinds[i_reap + 1:], ["read"])       # the post read starts only after the reap
        self.assertEqual(out["reaped"][0]["pids"], [4242]); self.assertEqual(out["reaped"][0]["when"], "before_post_read")

    def test_reader_killed_at_read_limit(self):
        import time as T, sys as S, usage_layer as UL
        old = LV._async_read.READ_WAIT_S; LV._async_read.READ_WAIT_S = 0.5
        seen = {}
        def reader(h, cancel=None):  # a real child on a pty that never answers
            seen["c"] = cancel
            try: return UL.pty_capture(["/bin/sleep", "30"], "/usage", boot_s=20, cancel=cancel)
            except UL.ReaderCancelled: seen["cancelled"] = True; raise
        try:
            t0 = T.time()
            rc, out = self._go(lambda h, **k: {"harness": h, "status": "done", "rc": 0}, reader)
        finally:
            LV._async_read.READ_WAIT_S = old
        self.assertEqual(rc, 0); self.assertLess(T.time() - t0, 10)
        k = out["reader_killed"][0]; pids = k["pre"] + k["post"]
        self.assertTrue(pids)
        T.sleep(0.5)
        for pid in pids:  # the reader's child is gone (killed and reaped by pty_capture's finally)
            self.assertFalse(os.path.exists("/proc/%d" % pid) and open("/proc/%d/stat" % pid).read().split(")")[1].split()[0] != "Z")
        row = self._ledger()[0]
        self.assertEqual((row["pre"]["state"], row["post"]["state"]), ("read_failed", "read_failed"))
        for _ in range(40):
            if seen.get("cancelled"): break
            T.sleep(0.1)
        self.assertTrue(seen.get("cancelled"))

    def test_offline_writes_nothing(self):
        def reader(h):
            raise AssertionError("reader")

        def call_fn(h, job=None, packet=None, **k):
            raise AssertionError("call")

        env = {"DISPATCH_STATE": self.state}
        rc, out = LV.run(self.pk, self.ledger, self.runs, self.capmap, self.caps,
                         udir=self.udir, env=env,
                         call_fn=call_fn, reader_fn=reader)
        self.assertEqual((rc, out["mode"]), (0, "offline"))
        self.assertNotIn("usage", out)
        for p in ("dispatch.log", "usage-ledger.jsonl", "usage", "LIVE"):
            self.assertFalse(os.path.exists(os.path.join(self.state, p)))

    def test_ledger_under_state_not_default(self):
        rc, out = self._go(lambda h, **k: {"harness": h, "status": "done"}, lambda h: self._rd(h, 50), pass_udir=False)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertTrue(os.path.isfile(os.path.join(self.state, "usage-ledger.jsonl")))
        self.assertFalse(os.path.exists(self.udir))

    def test_read_usage_config_gap_returns_none_pct(self):
        os.makedirs(self.udir)
        r = UW.read_usage("grok-build", udir=self.udir, now=1_700_000_000)
        self.assertEqual(set(r), {"harness", "state", "remaining_pct", "read_at"})
        self.assertEqual(r["harness"], "grok-build")
        self.assertEqual(r["state"], "config-gap")
        self.assertIsNone(r["remaining_pct"])
        self.assertNotIsInstance(r["remaining_pct"], (int, float))
        self.assertRegex(r["read_at"], r"T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertFalse(os.path.exists(os.path.join(self.udir, "grok-build.json")))
        with open(os.path.join(self.udir, "cursor.json"), "w", encoding="utf-8") as f:
            json.dump({"harness": "cursor", "remaining_pct": 12.5, "state": "counter"}, f)
        snap = UW.read_usage("cursor", udir=self.udir, now=1_700_000_000)
        self.assertEqual(snap["state"], "snapshot")
        self.assertEqual(snap["remaining_pct"], 12.5)
        self.assertEqual(snap["harness"], "cursor")

    def test_read_usage_missing_when_declared_and_no_snapshot(self):
        udir = tempfile.mkdtemp()
        old = UW.DECLARATIONS["agy-gemini"]
        UW.DECLARATIONS["agy-gemini"] = {
            "allowance": 10, "window_len_h": 1, "reset_at": "2026-01-01T00:00:00+00:00"}
        try:
            r = UW.read_usage("agy-gemini", udir=udir, now=1_700_000_000)
            self.assertEqual(r["state"], "missing")
            self.assertIsNone(r["remaining_pct"])
            self.assertFalse(os.path.exists(os.path.join(udir, "agy-gemini.json")))
        finally:
            UW.DECLARATIONS["agy-gemini"] = old
            shutil.rmtree(udir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
