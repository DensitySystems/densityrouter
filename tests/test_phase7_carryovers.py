"""Phase 7 carryovers and C1. Stdlib unittest.
Temp DISPATCH_STATE and DISPATCH_KILL. DISPATCH_LIVE is only ever an env dict passed to live.run.
Fake call_fn, except one test that clears harness_call.INVOKE for one harness,
calls the real harness_call.call, then restores INVOKE. subprocess.run stays patched to raise.
No network, no real harness.
Run: python3 tests/test_phase7_carryovers.py
"""
import copy, json, os, re, shutil, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D
import harness_call as HC
import live as LV
import usage_writer as UW

POOL = ["grok-build", "cursor", "agy-gemini"]


def _wall(y, mo, d, hh=12, mm=0, ss=0):
    return datetime(y, mo, d, hh, mm, ss).astimezone().replace(microsecond=0)


class Phase7Carryovers(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        self._decl = copy.deepcopy(UW.DECLARATIONS)
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL", "DISPATCH_LIVE")}
        os.environ["DISPATCH_STATE"] = self.tmp
        os.environ.pop("DISPATCH_LIVE", None)
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL")
        os.environ["DISPATCH_KILL"] = D.KILL
        self._run = HC.subprocess.run
        self._subprocess_run = subprocess.run

        def boom(*a, **k):
            raise AssertionError("subprocess.run")

        subprocess.run = boom
        HC.subprocess.run = boom
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = copy.deepcopy(D.load(os.path.join(ROOT, "caps.json")))
        self.assertIs(self.caps["cursor"]["enabled"], True)
        self.assertNotIn("daily_cap", self.caps["cursor"])
        self.assertNotIn("test_call_cap", self.caps["cursor"])
        self.assertEqual(self.caps["cursor"]["used_today"], 0)
        self.now = _wall(2026, 10, 25, 12)
        self.now_ts = self.now.timestamp()
        self.assertIsNone(D.HEALTH_COOLDOWN_S)
        self.assertIsNotNone(HC.INVOKE["cursor"])
        self.assertTrue(os.path.isabs(HC.INVOKE["cursor"][0]))
        self.assertEqual(D.WALK_STATUSES, {"failed_retryable", "timed_out", "failed_hold:usage", "failed_hold:not_installed"})

    def tearDown(self):
        subprocess.run = self._subprocess_run
        HC.subprocess.run = self._run
        D.KILL = self._kill
        UW.DECLARATIONS.clear()
        UW.DECLARATIONS.update(self._decl)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")

    def _udir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        return d

    def _put(self, udir, h, snap):
        with open(os.path.join(udir, h + ".json"), "w", encoding="utf-8") as f:
            json.dump(snap, f)
        return snap

    def _snap(self, h, rem_pct, window_h, hours_to_reset, burn=0, read_at=None):
        snap = {
            "harness": h,
            "remaining_pct": rem_pct,
            "window_len_h": window_h,
            "reset_at": (self.now + timedelta(hours=hours_to_reset)).isoformat(),
            "read_at": (self.now if read_at is None else read_at).isoformat(),
        }
        if burn is not None:
            snap["burn_pct_per_h"] = burn
        return snap

    def _route(self, text, udir):
        return D.route(text, self.capmap, self.caps, udir=udir, now=self.now_ts)

    def _bundle(self, objective, owner_name, sid):
        name = "PACKET-P7-%s.md" % sid
        path = os.path.join(self.tmp, name)
        text = "# PACKET: p7 %s\n\nObjective: %s\n" % (sid, objective)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        sha12 = D.sha256f(path)[:12]
        ledger = os.path.join(self.tmp, "ledger-%s.md" % sid)
        runs = os.path.join(self.tmp, "RUNS-%s.tsv" % sid)
        with open(ledger, "w", encoding="utf-8") as f:
            f.write("| 1 | GO | %s %s |\n" % (name, sha12))
        with open(runs, "w", encoding="utf-8") as f:
            f.write("x\t%s\n" % path)
        return path, ledger, runs

    def _live(self, path, ledger, runs, call_fn, pin=None):
        env = {"DISPATCH_LIVE": "1", "DISPATCH_STATE": os.environ["DISPATCH_STATE"]}
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "LIVE")))
        return LV.run(path, ledger, runs, self.capmap, self.caps, env=env,
                      call_fn=call_fn, pin=pin)

    def test_c1_not_dispatched_walks_on_to_callable(self):
        # No snapshots: refactor ability puts cursor first. The fake returns NOT_DISPATCHED
        # for that harness only; the next harness is a fake done. Cursor's real invoke path stays.
        path, ledger, runs = self._bundle(
            "Refactor the repo-wide helpers.", "Grok Build: c1", "feed0100")
        seen = []

        def call_fn(h, job=None, packet=None, **k):
            seen.append(h)
            if h == "cursor":
                return {"harness": h, "status": "NOT_DISPATCHED", "reason": "no invoke path on this host"}
            return {"harness": h, "status": "done"}

        rc, out = self._live(path, ledger, runs, call_fn)
        self.assertNotIn("NOT_DISPATCHED", D.WALK_STATUSES)
        self.assertIn("NOT_DISPATCHED", LV.LIVE_WALK)
        self.assertEqual(out["route"]["task_type"], "refactor")
        self.assertEqual(out["handoff"]["would_send_to"], "cursor")
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(seen, ["cursor", "grok-build"])
        self.assertEqual(out["handoff"]["walked"], ["cursor", "grok-build"])
        self.assertEqual([a["status"] for a in out["attempts"]], ["NOT_DISPATCHED", "done"])
        self.assertEqual(out["call"]["status"], "done")
        self.assertEqual(out["call"]["harness"], "grok-build")

    def test_c1_real_cursor_call_result_walks_on(self):
        # Cursor's production invoke path is real. Clear that one template, call the
        # real harness_call.call, and restore it. subprocess.run stays the setUp raise.
        path, ledger, runs = self._bundle(
            "Refactor the repo-wide helpers.", "Grok Build: c1real", "feed0101")
        saved = HC.INVOKE["cursor"]
        self.assertIsNotNone(saved)
        self.assertTrue(os.path.isabs(saved[0]))
        HC.INVOKE["cursor"] = None
        seen = []
        try:
            def call_fn(h, job=None, packet=None, **k):
                seen.append(h)
                if h == "cursor":
                    res = HC.call(h, job=job, packet=packet)
                    self.assertEqual(res["status"], HC.NOT_DISPATCHED)
                    self.assertNotIn("argv", res)
                    self.assertNotIn("argv0", res)
                    return res
                return {"harness": h, "status": "done"}

            rc, out = self._live(path, ledger, runs, call_fn)
            self.assertEqual(rc, 0, out["handoff"]["status"])
            self.assertEqual(seen, ["cursor", "grok-build"])
            self.assertEqual(out["handoff"]["walked"][0], "cursor")
            self.assertEqual(out["handoff"]["walked"][1], "grok-build")
            cursor = out["attempts"][0]
            self.assertEqual(cursor["harness"], "cursor")
            self.assertEqual(cursor["status"], HC.NOT_DISPATCHED)
            self.assertEqual(cursor["reason"], "no invoke path on this host")
            self.assertNotIn("argv", cursor)
            self.assertNotIn("argv0", cursor)
            self.assertEqual(out["call"]["status"], "done")
            self.assertEqual(out["call"]["harness"], "grok-build")
            self.assertIsNone(D.lease_held(out["handoff"]["job"]))
        finally:
            HC.INVOKE["cursor"] = saved
        self.assertIs(HC.INVOKE["cursor"], saved)

    def test_c1_all_uncallable_exits_6(self):
        path, ledger, runs = self._bundle(
            "Refactor the repo-wide helpers.", "Grok Build: c1all", "feed0102")
        seen = []

        def call_fn(h, job=None, packet=None, **k):
            seen.append(h)
            return {"harness": h, "status": "NOT_DISPATCHED", "reason": "no invoke path on this host"}

        rc, out = self._live(path, ledger, runs, call_fn)
        self.assertEqual(rc, 6)
        self.assertEqual(seen, ["cursor", "grok-build", "agy-gemini"])
        self.assertEqual(out["handoff"]["walked"], seen)
        self.assertTrue(all(a["status"] == "NOT_DISPATCHED" for a in out["attempts"]))
        self.assertEqual(out["call"]["status"], "NOT_DISPATCHED")
        self.assertNotIn(out["call"]["status"], ("done", "PENDING"))

    def test_c1_pinned_uncallable_exits_6_pin_only(self):
        path, ledger, runs = self._bundle(
            "Refactor the repo-wide helpers.", "Cursor: pin-only", "c0c0c0c0")
        seen = []

        def call_fn(h, job=None, packet=None, **k):
            seen.append(h)
            return {"harness": h, "status": "NOT_DISPATCHED", "reason": "no invoke path on this host"}

        rc, out = self._live(path, ledger, runs, call_fn, pin="cursor")
        self.assertEqual(rc, 6, out["handoff"]["status"])
        self.assertEqual(seen, ["cursor"])
        self.assertEqual(out["handoff"]["walked"], ["cursor"])
        self.assertEqual([a["harness"] for a in out["attempts"]], ["cursor"])
        self.assertEqual(out["call"]["status"], HC.NOT_DISPATCHED)
        self.assertNotIn("argv", out["call"])
        self.assertTrue(out["handoff"]["pinned"])
        self.assertEqual(out["handoff"]["would_send_to"], "cursor")
        self.assertGreater(len(out["route"]["ranked"]), 1)

    def test_cfg_gap_all_research_still_agy_gemini(self):
        # Oracle: research ability should still put agy-gemini first when every harness is config-gap.
        udir = self._udir("gap-research")
        text = "Objective: Research the eval notes, read-only.\n"
        r = self._route(text, udir)
        self.assertEqual(r["task_type"], "research")
        self.assertFalse(r["lan"])
        self.assertEqual([row["usage"] for row in r["ranked"]], ["config-gap"] * 3)
        self.assertTrue(all(row["U"] == 0.0 and row["H"] == 1.0 for row in r["ranked"]))
        self.assertEqual(r["basis"], "ability-only")
        self.assertEqual(r["ranked"][0]["harness"], "agy-gemini")

    def test_cfg_gap_all_refactor_order(self):
        # All config-gap: score on ability alone. cursor's refactor A is 0.9, above grok-build's 0.8.
        udir = self._udir("gap-refactor")
        r = self._route("Objective: Refactor the repo-wide helpers.\n", udir)
        self.assertEqual(r["task_type"], "refactor")
        self.assertFalse(r["lan"])
        self.assertEqual(r["basis"], "ability-only")
        self.assertEqual([row["harness"] for row in r["ranked"]],
                         ["cursor", "grok-build", "agy-gemini"])
        self.assertEqual(r["decided_by"], "score")
        self.assertTrue(all(row["usage"] == "config-gap" for row in r["ranked"]))
        cur = r["ranked"][0]
        gb = next(row for row in r["ranked"] if row["harness"] == "grok-build")
        self.assertEqual(cur["harness"], "cursor")
        self.assertGreater(cur["A"], gb["A"])
        self.assertEqual(cur["A"], 0.9)
        self.assertEqual(gb["A"], 0.8)
        self.assertGreater(cur["score"], gb["score"])

    def test_cfg_gap_tie_lowest_recent_use(self):
        # code_edit A is 0.9 for grok-build and cursor. The lower counter count wins.
        udir = self._udir("gap-tie")
        reset = (self.now + timedelta(hours=24)).isoformat()
        decl = {"allowance": 100, "window_len_h": 24, "reset_at": reset}
        gb_counter = UW.record_call("grok-build", n=5, now=self.now, decl=decl, udir=udir)
        cur_counter = UW.record_call("cursor", n=1, now=self.now, decl=decl, udir=udir)
        self.assertGreater(gb_counter["count"], cur_counter["count"])
        self.assertFalse(os.path.exists(os.path.join(udir, "grok-build.json")))
        self.assertFalse(os.path.exists(os.path.join(udir, "cursor.json")))
        r = self._route("Objective: Fix the build script.\n", udir)
        self.assertEqual(r["task_type"], "code_edit")
        self.assertEqual(r["basis"], "ability-only")
        self.assertTrue(all(row["usage"] == "config-gap" for row in r["ranked"]))
        self.assertEqual(r["ranked"][0]["harness"], "cursor")
        self.assertEqual(r["ranked"][1]["harness"], "grok-build")
        self.assertEqual(r["ranked"][0]["A"], r["ranked"][1]["A"])
        self.assertEqual(r["ranked"][0]["score"], r["ranked"][1]["score"])
        self.assertEqual(r["decided_by"], "score tie -> lowest recent use")

    def _s3(self, udir, grok_window, grok_reset_h):
        self._put(udir, "grok-build", self._snap("grok-build", 80, grok_window, grok_reset_h, burn=0))
        self._put(udir, "cursor", self._snap("cursor", 80, 168, 6 * 24, burn=5.0))
        return self._route("Objective: Refactor the repo-wide helpers.\n", udir)

    def test_s3_5h_window_recheck(self):
        # Same pair as S3, but the S3 winner (grok-build) has window_len_h 5 and reset inside 5h.
        udir = self._udir("s3-5h")
        r = self._s3(udir, 5, 4)
        with open(os.path.join(udir, "grok-build.json"), encoding="utf-8") as f:
            snap = json.load(f)
        self.assertEqual(snap["window_len_h"], 5)
        reset = datetime.fromisoformat(snap["reset_at"])
        self.assertGreater((reset - self.now).total_seconds(), 0)
        self.assertLessEqual((reset - self.now).total_seconds(), 5 * 3600)
        self.assertEqual(r["task_type"], "refactor")
        self.assertFalse(r["lan"])
        self.assertEqual(r["ranked"][0]["harness"], "grok-build")

    def _s7(self, udir, window_h, reset_h):
        h = "grok-build"
        UW.DECLARATIONS[h] = {
            "allowance": 100,
            "window_len_h": window_h,
            "reset_at": (self.now + timedelta(hours=reset_h)).isoformat(),
        }
        UW.record_call(h, n=20, now=self.now, udir=udir)
        snap = UW.write_snapshot(h, now=self.now, udir=udir)
        self._put(udir, "agy-gemini", self._snap("agy-gemini", 5, 5, 4, burn=0))
        return snap, self._route("Objective: Research the eval notes, read-only.\n", udir)

    def test_s7_5h_window_recheck(self):
        # Same pair as S7, but the S7 winner (grok-build) has window_len_h 5 and reset inside 5h.
        udir = self._udir("s7-5h")
        snap, r = self._s7(udir, 5, 4.5)
        self.assertEqual(snap["window_len_h"], 5)
        self.assertEqual(snap["source"], "own-counter")
        reset = datetime.fromisoformat(snap["reset_at"])
        self.assertGreater((reset - self.now).total_seconds(), 0)
        self.assertLessEqual((reset - self.now).total_seconds(), 5 * 3600)
        self.assertEqual(r["task_type"], "research")
        self.assertFalse(r["lan"])
        gb = next(row for row in r["ranked"] if row["harness"] == "grok-build")
        agy = next(row for row in r["ranked"] if row["harness"] == "agy-gemini")
        self.assertEqual(gb["H"], agy["H"])
        names = [row["harness"] for row in r["ranked"]]
        self.assertLess(names.index("grok-build"), names.index("agy-gemini"))

    def test_s7_window_24h(self):
        udir = self._udir("s7-24h")
        snap, r = self._s7(udir, 24, 20)
        self.assertEqual(snap["window_len_h"], 24)
        self.assertEqual(r["task_type"], "research")
        self.assertFalse(r["lan"])
        self.assertEqual(r["basis"], "usage")
        gb = next(row for row in r["ranked"] if row["harness"] == "grok-build")
        agy = next(row for row in r["ranked"] if row["harness"] == "agy-gemini")
        self.assertEqual(gb["H"], agy["H"])
        names = [row["harness"] for row in r["ranked"]]
        self.assertLess(names.index("grok-build"), names.index("agy-gemini"))

    def test_s7_window_168h(self):
        udir = self._udir("s7-168h")
        snap, r = self._s7(udir, 168, 96)
        self.assertEqual(snap["window_len_h"], 168)
        self.assertEqual(r["task_type"], "research")
        self.assertFalse(r["lan"])
        self.assertEqual(r["basis"], "usage")
        gb = next(row for row in r["ranked"] if row["harness"] == "grok-build")
        agy = next(row for row in r["ranked"] if row["harness"] == "agy-gemini")
        self.assertEqual(gb["H"], agy["H"])
        names = [row["harness"] for row in r["ranked"]]
        self.assertLess(names.index("grok-build"), names.index("agy-gemini"))

    def test_5h_near_reset_beats_168h(self):
        # Equal code_edit A (0.9) and H. Same remaining 50%, burn 0.
        # cursor: w=5, reset in 0.5h. grok-build: w=168, reset in 96h.
        # The 5h window is nearer reset, so its U is higher and it ranks first.
        udir = self._udir("near-reset")
        self._put(udir, "cursor", self._snap("cursor", 50, 5, 0.5, burn=0))
        self._put(udir, "grok-build", self._snap("grok-build", 50, 168, 96, burn=0))
        with open(os.path.join(udir, "cursor.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["window_len_h"], 5)
        with open(os.path.join(udir, "grok-build.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["window_len_h"], 168)
        r = self._route("Objective: Fix the build script.\n", udir)
        self.assertEqual(r["task_type"], "code_edit")
        short = next(row for row in r["ranked"] if row["harness"] == "cursor")
        long = next(row for row in r["ranked"] if row["harness"] == "grok-build")
        self.assertEqual(short["A"], long["A"])
        self.assertEqual(short["H"], long["H"])
        self.assertGreater(short["U"], long["U"])
        self.assertEqual(r["ranked"][0]["harness"], "cursor")
        self.assertNotIn("async-fake", [row["harness"] for row in r["ranked"]])

    def test_dispatcher_uses_public_usage_writer_helpers(self):
        with open(os.path.join(ROOT, "dispatcher.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(set(re.findall(r"\bUW\.(_[A-Za-z0-9_]+)", src)), set())
        for name in ("missing", "fields_for", "read_json", "counter_path", "coerce_dt", "is_unset"):
            self.assertIn("UW.%s" % name, src)
            self.assertTrue(hasattr(UW, name), name)
            self.assertTrue(hasattr(UW, "_" + name), name)

    def test_poll_async_does_not_rewrite_pending_file(self):
        job = "stale-pending-p7"
        now = 1_700_000_000.0
        path = HC.pending_path(job)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {
            "harness": "async-fake",
            "job": job,
            "status": "STALE",
            "stale": True,
            "deadline": now - 60,
            "submitted": now - 3600,
            "receipt": None,
            "marker": "keep-these-bytes",
        }
        raw = json.dumps(rec).encode("utf-8")
        with open(path, "wb") as f:
            f.write(raw)
        first = HC.poll_async(job, now=now)
        second = HC.poll_async(job, now=now + 30)
        self.assertEqual(first["status"], "STALE")
        self.assertEqual(second["status"], "STALE")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_lease_held_partial_receipt_holds(self):
        # A partial receipt (bytes, but no Result: line) does not free the lease.
        now = 5_000_000.0
        job = "partial-receipt"
        receipt = os.path.join(self.tmp, "partial-receipt.md")
        ok, _msg = D.lease_acquire(job, "grok-build", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "grok-build", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        self.assertIsNotNone(D.lease_held(job, now=now))
        with open(receipt, "wb") as f:
            f.write(b"PAR")
        self.assertGreater(os.path.getsize(receipt), 0)
        self.assertIsNotNone(D.lease_held(job, now=now))

    def test_lease_held_oserror_holds(self):
        # OSError while checking the receipt does not free the lease.
        now = 5_000_000.0
        job = "oserror-receipt"
        receipt = os.path.join(self.tmp, "oserror-receipt.md")
        ok, _msg = D.lease_acquire(job, "async-fake", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "async-fake", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        self.assertIsNotNone(D.lease_held(job, now=now))
        real = os.path.exists

        def exists(path):
            if path == receipt:
                raise OSError("stat failed")
            return real(path)

        os.path.exists = exists
        try:
            self.assertIsNotNone(D.lease_held(job, now=now))
        finally:
            os.path.exists = real

    def test_enabled_false_is_inert(self):
        caps = copy.deepcopy(self.caps)
        for h in POOL:
            caps[h]["enabled"] = False
        udir = self._udir("enabled")
        r = D.route("Objective: Fix the build script.\n", self.capmap, caps, udir=udir, now=self.now_ts)
        self.assertEqual([row["harness"] for row in r["ranked"]], POOL)
        self.assertEqual(r["skipped"], [])
        self.assertNotIn("cursor", [h for h, _why in r["skipped"]])
        self.assertNotIn("grok-build", [h for h, _why in r["skipped"]])

    def test_pinned_failure_exits_6_pin_only(self):
        path, ledger, runs = self._bundle(
            "Fix the build script.", "Grok Build: pin-fail", "feed0103")
        seen = []

        def call_fn(h, job=None, packet=None, **k):
            seen.append(h)
            return {"harness": h, "status": "failed_retryable"}

        rc, out = self._live(path, ledger, runs, call_fn, pin="grok-build")
        self.assertEqual(rc, 6, out["handoff"]["status"])
        self.assertEqual(seen, ["grok-build"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build"])
        self.assertEqual([a["harness"] for a in out["attempts"]], ["grok-build"])
        self.assertEqual(out["call"]["status"], "failed_retryable")
        self.assertTrue(out["handoff"]["pinned"])
        self.assertEqual(out["handoff"]["would_send_to"], "grok-build")
        self.assertGreater(len(out["route"]["ranked"]), 1)
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
