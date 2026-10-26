"""Phase 4: simple health cooldown. stdlib unittest, temp DISPATCH_STATE only.
No real harness is called. Run: python3 tests/test_health_cooldown.py
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D
import harness_call as HC

CAPS_SHA_PREFIX = "02a81b2eb0a8"
# Test-only cooldown length. HEALTH_COOLDOWN_S stays unset.
N_FIXTURE = 60
TEXT = "fix the code"
GAP = "HEALTH_COOLDOWN_S unset, cooldown not applied"


class _Proc:
    def __init__(self, rc, out="OK", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _sha_caps():
    h = hashlib.sha256()
    with open(os.path.join(ROOT, "caps.json"), "rb") as f:
        h.update(f.read())
    return h.hexdigest()


class HealthCooldownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL")}
        os.environ["DISPATCH_STATE"] = self.tmp
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL")
        os.environ["DISPATCH_KILL"] = D.KILL
        self._cooldown = D.HEALTH_COOLDOWN_S
        self._read_health = D.read_health
        self._run = HC.subprocess.run
        self._subprocess_run = subprocess.run
        self.subprocess_calls = []

        def boom(*a, **k):
            self.subprocess_calls.append(1)
            raise AssertionError("subprocess.run")

        subprocess.run = boom
        HC.subprocess.run = boom
        self.cap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = D.load(os.path.join(ROOT, "caps.json"))

    def tearDown(self):
        subprocess.run = self._subprocess_run
        HC.subprocess.run = self._run
        D.read_health = self._read_health
        D.HEALTH_COOLDOWN_S = self._cooldown
        D.KILL = self._kill
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _route(self, now, cooldown_s=None, health=None):
        kw = {}
        if cooldown_s is not None:
            kw["cooldown_s"] = cooldown_s
        if health is not None:
            kw["health"] = health
        return D.route(TEXT, self.cap, self.caps, now=now, **kw)

    def _why(self, result, harness):
        hits = [why for h, why in result["skipped"] if h == harness]
        self.assertEqual(len(hits), 1, result["skipped"])
        return hits[0]

    def test_health_record_has_class_and_timestamp(self):
        self.assertEqual(HC.read_health(), {})
        self.assertEqual(D.read_health(os.path.join(self.tmp, "missing.json")), {})
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            f.write("{")
        self.assertEqual(HC.read_health(bad), {})
        self.assertEqual(D.read_health(self.tmp), {})

        def timed_out(argv, **k):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=k.get("timeout") or 60)

        HC.subprocess.run = timed_out
        before = time.time()
        got = HC.call("agy-gemini", ["agy", "-p", "x"])
        after = time.time()
        self.assertEqual(got["status"], "timed_out")
        path = D.health_path()
        self.assertEqual(path, os.path.join(self.tmp, "health.json"))
        self.assertFalse(os.path.exists(path + ".tmp"))
        with open(path) as f:
            disk = json.load(f)
        rec = disk["agy-gemini"]
        self.assertEqual(set(rec), {"class", "ts"})
        self.assertEqual(rec["class"], "timed_out")
        self.assertIsInstance(rec["ts"], float)
        self.assertGreaterEqual(rec["ts"], before)
        self.assertLessEqual(rec["ts"], after)
        self.assertEqual(HC.read_health(), disk)

    def test_health_written_on_sync_and_async_status(self):
        def failed(argv, **k):
            return _Proc(1, "boom", "")

        HC.subprocess.run = failed
        sync = HC.call("agy-gemini", ["agy", "-p", "x"])
        self.assertEqual(sync["status"], "failed_retryable")
        agy = HC.read_health()["agy-gemini"]
        self.assertEqual(agy["class"], "failed_retryable")
        self.assertIsInstance(agy["ts"], float)

        t0 = 1_700_000_000.0
        pending = HC.submit_async("async-fake", "job-s", now=t0, deadline_min=30)
        self.assertEqual(pending["status"], "PENDING")
        self.assertNotIn("async-fake", HC.read_health())
        stale = HC.poll_async("job-s", now=t0 + 30 * 60 + 1)
        self.assertEqual(stale["status"], "STALE")
        self.assertEqual(HC.read_health()["async-fake"], {"class": "STALE", "ts": t0 + 30 * 60})  # deadline, not the poll time
        self.assertEqual(HC.read_health()["agy-gemini"]["class"], "failed_retryable")

        receipt = os.path.join(self.tmp, "receipt.txt")
        with open(receipt, "w") as f:
            f.write("finished ok\n")
        queued = HC.submit_async("async-fake", "job-d", receipt=receipt, now=t0)
        self.assertEqual(queued["status"], "PENDING")
        done = HC.poll_async("job-d", now=t0 + 5)
        self.assertEqual(done["status"], "done")
        self.assertEqual(HC.read_health()["async-fake"], {"class": "done", "ts": t0 + 5})
        self.assertEqual(HC.read_health()["agy-gemini"]["class"], "failed_retryable")

    def test_done_clears_cooldown(self):
        ts = 1_700_000_000.0
        HC.record_health("agy-gemini", "timed_out", now=ts)
        inside = self._route(ts + 10, cooldown_s=N_FIXTURE)
        self.assertIn("health: cooldown timed_out", self._why(inside, "agy-gemini"))
        self.assertNotIn("agy-gemini", [row["harness"] for row in inside["ranked"]])

        def ok(argv, **k):
            return _Proc(0, "OK", "")

        HC.subprocess.run = ok
        got = HC.call("agy-gemini", ["agy", "-p", "ok"])
        self.assertEqual(got["status"], "done")
        rec = HC.read_health()["agy-gemini"]
        self.assertEqual(rec["class"], "done")
        self.assertIsInstance(rec["ts"], float)
        self.assertFalse(D.health_skip("agy-gemini", rec, rec["ts"] + 1, N_FIXTURE))
        after = self._route(rec["ts"] + 1, cooldown_s=N_FIXTURE)
        self.assertNotIn("agy-gemini", [h for h, _why in after["skipped"]])
        row = next(r for r in after["ranked"] if r["harness"] == "agy-gemini")
        self.assertEqual(row["H"], 1.0)
        self.assertIsNotNone(row["score"])

    def test_pending_does_not_start_cooldown(self):
        HC.record_health("grok-build", "PENDING")
        HC.record_health("grok-build", "REFUSED")
        HC.record_health("grok-build", "BLOCKED")
        HC.record_health("grok-build", "UNKNOWN")
        HC.record_health("grok-build", None)
        self.assertFalse(os.path.exists(D.health_path()))
        t0 = 1_000_000.0
        rec = HC.submit_async("async-fake", "pend", now=t0)
        self.assertEqual(rec["status"], "PENDING")
        self.assertFalse(os.path.exists(D.health_path()))
        HC.record_health("async-fake", "timed_out", now=50.0)
        with open(D.health_path(), "rb") as f:
            before = f.read()
        polled = HC.poll_async("pend", now=t0)
        self.assertEqual(polled["status"], "PENDING")
        missing = HC.call("agy-gemini", argv=[])
        self.assertEqual(missing["status"], "NOT_DISPATCHED")
        self.assertEqual(missing["reason"], "no invoke path on this host")
        with open(D.health_path(), "rb") as f:
            after = f.read()
        self.assertEqual(after, before)
        self.assertNotIn("agy-gemini", HC.read_health())
        self.assertEqual(HC.read_health()["async-fake"]["class"], "timed_out")

    def test_local_refuse_does_not_write_failure(self):
        HC.record_health("grok-build", "timed_out", now=11.0)
        HC.record_health("agy-gemini", "failed_retryable", now=12.0)
        HC.record_health("async-fake", "cancelled", now=13.0)
        path = D.health_path()
        with open(path, "rb") as f:
            before = f.read()
        blocked = HC.call("cursor")
        self.assertEqual((blocked["status"], blocked["reason"]), ("REFUSED", "no packet"))  # cursor invoke path is real: no smoke ping
        self.assertNotEqual(blocked["status"], "BLOCKED")
        no_packet = HC.call("grok-build")
        self.assertEqual(no_packet["status"], "REFUSED")
        self.assertIn("no packet", no_packet["reason"])
        flagged = HC.call("grok-build", ["grok", "-p", "x", "--dangerously-skip-permissions"])
        self.assertEqual(flagged["status"], "REFUSED")
        self.assertIsNone(HC.CAPS["grok-build"])  # missing cap is no ceiling; do not range() it
        secret = os.path.join(self.tmp, "secret.md")
        with open(secret, "w") as f:
            f.write("token " + "sk" + "-testkey1234567890\n")
        refused = HC.submit_async("async-fake", "redact-job", packet=secret)
        self.assertEqual(refused["status"], "REFUSED")
        via_call = HC.call("async-fake", job="redact-call", packet=secret)
        self.assertEqual(via_call["status"], "REFUSED")
        with open(path, "rb") as f:
            after = f.read()
        self.assertEqual(after, before)
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertNotIn("cursor", HC.read_health())
        self.assertEqual(self.subprocess_calls, [])

    def test_cooldown_skips_harness_until_n(self):
        self.assertIsNone(D.HEALTH_COOLDOWN_S)
        ts = 1_700_000_000.0
        HC.record_health("agy-gemini", "timed_out", now=ts)
        HC.record_health("cursor", "failed_retryable", now=ts)
        stored = HC.read_health()["agy-gemini"]["ts"]
        self.assertTrue(D.health_skip("agy-gemini", {"class": "timed_out", "ts": stored}, stored + N_FIXTURE - 1, N_FIXTURE))
        self.assertFalse(D.health_skip("agy-gemini", {"class": "timed_out", "ts": stored}, stored + N_FIXTURE, N_FIXTURE))
        self.assertFalse(D.health_skip("agy-gemini", {"class": "timed_out", "ts": stored}, stored, None))
        self.assertFalse(D.health_skip("agy-gemini", {"class": "done", "ts": stored}, stored, N_FIXTURE))

        inside = self._route(stored + N_FIXTURE - 1, cooldown_s=N_FIXTURE)
        cursor_why = self._why(inside, "cursor")
        self.assertEqual(cursor_why, "health: cooldown failed_retryable until %s" % (stored + N_FIXTURE))
        self.assertNotIn("hard: disabled", cursor_why)
        agy_why = self._why(inside, "agy-gemini")
        self.assertEqual(agy_why, "health: cooldown timed_out until %s" % (stored + N_FIXTURE))
        self.assertNotIn("agy-gemini", [row["harness"] for row in inside["ranked"]])
        self.assertEqual([h for h, _why in inside["skipped"]].index("cursor"), 0)
        live = next(row for row in inside["ranked"] if row["harness"] == "grok-build")
        self.assertEqual(live["H"], 1.0)
        self.assertIsNotNone(live["score"])
        self.assertIsNone(inside["health_gap"])

        past = self._route(stored + N_FIXTURE, cooldown_s=N_FIXTURE)
        self.assertNotIn("agy-gemini", [h for h, _why in past["skipped"]])
        row = next(r for r in past["ranked"] if r["harness"] == "agy-gemini")
        self.assertEqual(row["H"], 1.0)
        self.assertIsNotNone(row["score"])
        self.assertEqual(HC.read_health()["agy-gemini"]["class"], "timed_out")
        self.assertIsNone(D.HEALTH_COOLDOWN_S)

    def test_route_choose_sees_health_file(self):
        ts = 1_800_000_000.0
        HC.record_health("grok-build", "cancelled", now=ts)
        with open(os.path.join(ROOT, "dispatcher.py")) as f:
            src = f.read()
        self.assertIn("r = route(pt, capmap, caps, x.usage_dir)", src)
        # route-choose passes no health argument, so the file is the input.
        result = D.route(TEXT, self.cap, self.caps, None, now=ts + 1, cooldown_s=N_FIXTURE)
        why = self._why(result, "grok-build")
        stored = HC.read_health()["grok-build"]["ts"]
        self.assertEqual(why, "health: cooldown cancelled until %s" % (stored + N_FIXTURE))
        self.assertNotIn("grok-build", [row["harness"] for row in result["ranked"]])
        self.assertIsNone(result["health_gap"])
        self.assertNotIn("cursor", [h for h, _why in result["skipped"]])
        self.assertIn("cursor", [row["harness"] for row in result["ranked"]])

    def test_no_probe_subprocess(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise AssertionError("subprocess.run")

        subprocess.run = boom
        HC.subprocess.run = boom
        HC.record_health("grok-build", "failed_retryable", now=3.0)
        self._route(4.0, cooldown_s=10)
        self._route(4.0)
        self.assertEqual(calls, [])
        self.assertEqual(self.subprocess_calls, [])
        self.assertEqual(HC.read_health()["grok-build"]["class"], "failed_retryable")

    def test_caps_json_unchanged_phase_4(self):
        before = _sha_caps()
        HC.record_health("grok-build", "timed_out", now=1.0)
        self._route(2.0, cooldown_s=10)
        self._route(2.0)
        after = _sha_caps()
        self.assertEqual(before, after)
        self.assertTrue(after.startswith(CAPS_SHA_PREFIX))

    def test_n_unset_no_skip_reports_gap(self):
        self.assertIsNone(D.HEALTH_COOLDOWN_S)
        empty = self._route(1.0)
        self.assertIsNone(empty["health_gap"])
        self.assertFalse(any(why.startswith("health:") for _h, why in empty["skipped"]))
        ts = 1_700_000_100.0
        HC.record_health("agy-gemini", "cancelled", now=ts)
        result = D.route(TEXT, self.cap, self.caps, now=ts + 1)
        self.assertIsNone(D.HEALTH_COOLDOWN_S)
        self.assertNotIn("agy-gemini", [h for h, _why in result["skipped"]])
        row = next(r for r in result["ranked"] if r["harness"] == "agy-gemini")
        self.assertEqual(row["H"], 1.0)
        self.assertIsNotNone(row["score"])
        self.assertEqual(result["health_gap"], GAP)
        HC.record_health("agy-gemini", "done", now=ts + 2)
        cleared = D.route(TEXT, self.cap, self.caps, now=ts + 3)
        self.assertIsNone(cleared["health_gap"])
        self.assertNotIn("agy-gemini", [h for h, _why in cleared["skipped"]])

    def test_injected_health_overrides_file(self):
        ts = 1_700_000_000.0
        HC.record_health("agy-gemini", "timed_out", now=ts)
        def boom(*a, **k):
            raise AssertionError("health file read")
        D.read_health = boom
        try:
            result = self._route(ts + 1, cooldown_s=10_000, health={"agy-gemini": 0.25})
        finally:
            D.read_health = self._read_health
        self.assertNotIn("health_gap", result)
        self.assertNotIn("agy-gemini", [h for h, _why in result["skipped"]])
        row = next(r for r in result["ranked"] if r["harness"] == "agy-gemini")
        self.assertEqual(row["H"], 0.25)
        u_raw, _state = D.usage_for("agy-gemini", None, now=ts + 1)
        a = self.cap["agy-gemini"]["code_edit"]
        self.assertEqual(row["score"], round((a ** 0.6) * (u_raw ** 0.4) * 0.25, 4))
        self.assertNotIn("cursor", [h for h, _why in result["skipped"]])
        self.assertIn("cursor", [row["harness"] for row in result["ranked"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
