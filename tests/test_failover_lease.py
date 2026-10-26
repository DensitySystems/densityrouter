"""Phase 5: walk retryable failures, and hold a lease only for a real async wait.
Stdlib unittest. Temp DISPATCH_STATE only. DISPATCH_LIVE=1 is an env dict, never os.environ.
Fake call_fn. subprocess.run is patched to raise. No real harness.
Run: python3 tests/test_failover_lease.py
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, live as LV, harness_call as HC

CAPS_SHA_PREFIX = "02a81b2eb0a8"
TEXT = (
    "# PACKET: walk\n"
    "\n"
    "## Objective\n"
    "Fix a script in the scratch dir.\n"
)


class FailoverLease(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
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
        self.text = TEXT
        self.pk = os.path.join(self.tmp, "PACKET-WALK-r1.md")
        with open(self.pk, "w", encoding="utf-8") as f:
            f.write(self.text)
        sha12 = D.sha256f(self.pk)[:12]
        self.ledger = os.path.join(self.tmp, "ledger.md")
        self.runs = os.path.join(self.tmp, "RUNS.tsv")
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write("| 1 | GO | PACKET-WALK-r1.md %s |\n" % sha12)
        with open(self.runs, "w", encoding="utf-8") as f:
            f.write("x\t%s\n" % self.pk)
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = D.load(os.path.join(ROOT, "caps.json"))
        ranked = [r["harness"] for r in D.route(self.text, self.capmap, self.caps)["ranked"] if r["score"] is not None]
        self.assertEqual(ranked, ["grok-build", "cursor", "agy-gemini"])
        self.assertNotIn("async-fake", ranked)
        self.assertEqual(D.WALK_STATUSES, {"failed_retryable", "timed_out", "failed_hold:usage", "failed_hold:not_installed"})
        self.assertIsNone(D.HEALTH_COOLDOWN_S)

    def tearDown(self):
        subprocess.run = self._subprocess_run
        HC.subprocess.run = self._run
        D.KILL = self._kill
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")

    def go(self, call_fn, pin=None):
        env = {"DISPATCH_LIVE": "1", "DISPATCH_STATE": os.environ["DISPATCH_STATE"]}
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        return LV.run(self.pk, self.ledger, self.runs, self.capmap, self.caps,
                      env=env, call_fn=call_fn, pin=pin)

    def _returning(self, statuses, seen=None, **extra):
        def call_fn(h, job=None, packet=None, **k):
            if seen is not None:
                seen.append((h, job, packet))
            if isinstance(statuses, dict):
                st = statuses[h]
            else:
                st = statuses
            out = {"harness": h, "status": st}
            out.update(extra)
            return out
        return call_fn

    def _lease_path(self, job):
        return os.path.join(D.lease_dir(), "%s.json" % job)

    def _pending(self, job, deadline, **fields):
        path = os.path.join(self.tmp, "pending", "%s.json" % job)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {"harness": "async-fake", "job": job, "status": "PENDING", "deadline": deadline, "stale": False}
        rec.update(fields)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        return rec

    def test_walk_on_failed_retryable(self):
        seen = []
        statuses = {"grok-build": "failed_retryable", "cursor": "failed_retryable", "agy-gemini": "done"}
        rc, out = self.go(self._returning(statuses, seen))
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual([h for h, _j, _p in seen], ["grok-build", "cursor", "agy-gemini"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build", "cursor", "agy-gemini"])
        self.assertEqual([a["status"] for a in out["attempts"]], ["failed_retryable", "failed_retryable", "done"])
        self.assertEqual(out["call"]["status"], "done")
        self.assertEqual(out["call"]["harness"], "agy-gemini")
        self.assertIn("ROUTED LIVE -> agy-gemini (done)", out["handoff"]["status"])
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))

    def test_walk_on_timed_out(self):
        seen = []
        statuses = {"grok-build": "timed_out", "cursor": "done", "agy-gemini": "done"}
        rc, out = self.go(self._returning(statuses, seen))
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual([h for h, _j, _p in seen], ["grok-build", "cursor"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build", "cursor"])
        self.assertEqual(out["call"]["status"], "done")
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))

    def test_walk_on_failed_hold_usage(self):
        seen = []
        statuses = {"grok-build": "failed_hold:usage", "cursor": "done", "agy-gemini": "done"}
        rc, out = self.go(self._returning(statuses, seen))
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual([h for h, _j, _p in seen], ["grok-build", "cursor"])
        self.assertEqual([a["status"] for a in out["attempts"]], ["failed_hold:usage", "done"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build", "cursor"])
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))

    def test_stop_on_done(self):
        seen = []
        def call_fn(h, job=None, packet=None, **k):
            seen.append(h)
            self.assertEqual(packet, self.pk)
            return {"harness": h, "status": "done", "stdout": "Bearer abcdefghijklmnop"}
        rc, out = self.go(call_fn)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(seen, ["grok-build"])
        self.assertGreater(len(out["route"]["ranked"]), 1)
        self.assertEqual(out["handoff"]["walked"], ["grok-build"])
        self.assertEqual(out["call"]["status"], "done")
        self.assertNotIn("abcdefghijklmnop", json.dumps(out))
        self.assertGreaterEqual(out["redaction_hits"], 1)
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))

    def test_stop_on_real_pending(self):
        seen = []
        deadline = 9_000_000_000.0
        def call_fn(h, job=None, packet=None, **k):
            seen.append(h)
            return {"harness": h, "status": "PENDING", "deadline": deadline, "job": job}
        rc, out = self.go(call_fn)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(seen, ["grok-build"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build"])
        self.assertEqual(out["call"]["status"], "PENDING")
        self.assertIn("ROUTED LIVE -> grok-build (PENDING)", out["handoff"]["status"])
        held = D.lease_held(out["handoff"]["job"])
        self.assertTrue(held)
        self.assertEqual(held["expiry"], deadline)

    def test_no_walk_on_cancelled(self):
        # failed_hold:not_installed walks (missing binary). cancelled still stops.
        seen = []
        rc, out = self.go(self._returning("cancelled", seen))
        self.assertEqual(rc, 6)
        self.assertEqual([h for h, _j, _p in seen], ["grok-build"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build"])
        self.assertEqual(out["call"]["status"], "cancelled")
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))
        self.assertNotIn("agy-gemini", out["handoff"]["walked"])

    def test_not_dispatched_is_not_exit_0(self):
        # C1: NOT_DISPATCHED does not stop an unpinned walk. Every candidate here is uncallable, so the exit stays 6.
        seen = []
        rc, out = self.go(self._returning("NOT_DISPATCHED", seen))
        self.assertNotEqual(rc, 0)
        self.assertEqual(rc, 6)
        self.assertNotIn("NOT_DISPATCHED", D.WALK_STATUSES)
        self.assertEqual([h for h, _j, _p in seen], ["grok-build", "cursor", "agy-gemini"])
        self.assertEqual(out["call"]["status"], "NOT_DISPATCHED")
        self.assertEqual(out["call"]["harness"], "agy-gemini")
        self.assertEqual(out["handoff"]["walked"], ["grok-build", "cursor", "agy-gemini"])
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))
        blocked = HC.call("cursor")
        self.assertEqual((blocked["status"], blocked["reason"]), ("REFUSED", "no packet"))  # cursor invoke path is real: no smoke ping
        self.assertNotEqual(blocked["status"], "BLOCKED")
        missing = HC.call("agy-gemini", argv=[])
        self.assertEqual(missing["status"], HC.NOT_DISPATCHED)
        self.assertEqual(missing["reason"], "no invoke path on this host")
        self.assertNotIn(missing["status"], ("done", "PENDING"))
        self.assertNotIn("agy-gemini", HC.read_health())

    def test_lease_released_before_next_sync_attempt(self):
        seen = []
        def call_fn(h, job=None, packet=None, **k):
            path = self._lease_path(job)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                owner = json.load(f).get("owner")
            self.assertEqual(owner, h)
            if seen:
                # The previous harness released before this call. Its lease is gone.
                self.assertNotEqual(owner, seen[-1], "lease file from the previous harness still exists")
            seen.append(h)
            if h == "grok-build":
                return {"harness": h, "status": "failed_retryable"}
            return {"harness": h, "status": "done"}
        rc, out = self.go(call_fn)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(seen, ["grok-build", "cursor"])
        job = out["handoff"]["job"]
        self.assertFalse(os.path.exists(self._lease_path(job)))
        self.assertIsNone(D.lease_held(job))
        def boom(h, job=None, packet=None, **k):
            raise RuntimeError("call failed")
        with self.assertRaises(RuntimeError):
            self.go(boom)
        self.assertIsNone(D.lease_held(job))
        self.assertFalse(os.path.exists(self._lease_path(job)))

    def test_lease_held_until_async_receipt_or_deadline(self):
        deadline = 9_000_000_000.0
        receipt = os.path.join(self.tmp, "async-receipt.md")
        seen = []
        def call_fn(h, job=None, packet=None, **k):
            seen.append(h)
            return {"harness": h, "status": "PENDING", "deadline": deadline, "receipt": receipt, "job": job}
        rc, out = self.go(call_fn)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(seen, ["grok-build"])
        job = out["handoff"]["job"]
        self.assertFalse(os.path.exists(receipt))
        held = D.lease_held(job)
        self.assertTrue(held)
        self.assertEqual(held["expiry"], deadline)
        self.assertIsNotNone(D.lease_held(job, now=deadline - 1))
        blocked = D.route(self.text, self.capmap, self.caps, job=job, now=deadline - 1)
        self.assertEqual(blocked["ranked"], [])
        self.assertEqual(blocked["decided_by"], "hard: lease held -> no reroute")
        self.assertIsNone(D.lease_held(job, now=deadline + 1))
        self.assertIsNone(D.lease_held(job, now=deadline))
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("Result: done\n")
        lease = D.load(os.path.join(D.lease_dir(), "%s.json" % job))
        os.utime(receipt, (lease["acquired"] + 1, lease["acquired"] + 1))
        self.assertIsNone(D.lease_held(job, now=deadline - 1))
        opened = D.route(self.text, self.capmap, self.caps, job=job, now=deadline - 1)
        self.assertTrue(opened["ranked"])

    def test_not_dispatched_holds_no_lease(self):
        # Fake NOT_DISPATCHED for every harness. Cursor's real invoke path is not used here.
        saw = []
        def call_fn(h, job=None, packet=None, **k):
            saw.append(os.path.exists(self._lease_path(job)))
            return {"harness": h, "status": "NOT_DISPATCHED", "reason": "no invoke path on this host"}
        rc, out = self.go(call_fn)
        job = out["handoff"]["job"]
        self.assertEqual(rc, 6)
        self.assertEqual(saw, [True, True, True])
        self.assertEqual(out["handoff"]["walked"], ["grok-build", "cursor", "agy-gemini"])
        got = next(a for a in out["attempts"] if a["harness"] == "cursor")
        self.assertEqual(got["status"], HC.NOT_DISPATCHED)
        self.assertEqual(got["reason"], "no invoke path on this host")
        self.assertNotIn("argv", got)
        self.assertNotIn("argv0", got)
        self.assertFalse(os.path.exists(self._lease_path(job)))
        self.assertIsNone(D.lease_held(job))

    def test_pending_file_blocks_route_without_lease_file(self):
        job = "pend-only"
        now = 5_000_000.0
        path = os.path.join(self.tmp, "pending", "%s.json" % job)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        def write(deadline, **fields):
            rec = {"harness": "async-fake", "job": job, "status": "PENDING", "deadline": deadline, "stale": False}
            rec.update(fields)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rec, f)
            return rec
        write(now + 1000)
        lease = self._lease_path(job)
        self.assertFalse(os.path.exists(lease))
        with open(path, "rb") as f:
            raw = f.read()
        self.assertIsNotNone(D.pending_held(job, now=now))
        self.assertIsNone(D.lease_held(job, now=now))
        blocked = D.route(self.text, self.capmap, self.caps, job=job, now=now)
        self.assertEqual(blocked["ranked"], [])
        self.assertEqual(blocked["decided_by"], "hard: lease held -> no reroute")
        self.assertFalse(os.path.exists(lease))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw)
        write(now - 1)
        self.assertIsNone(D.pending_held(job, now=now))
        self.assertTrue(D.route(self.text, self.capmap, self.caps, job=job, now=now)["ranked"])
        write(now)
        self.assertIsNone(D.pending_held(job, now=now))
        receipt = os.path.join(self.tmp, "pend-receipt.md")
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("Result: done\n")
        # A path on disk is not enough: the receipt must be complete for this submit.
        write(now + 5000, receipt=receipt, submitted=now - 10)
        self.assertIsNone(D.pending_held(job, now=now))
        self.assertTrue(D.route(self.text, self.capmap, self.caps, job=job, now=now)["ranked"])
        missing = os.path.join(self.tmp, "no-such-receipt.md")
        write(now + 5000, receipt=missing)
        self.assertIsNotNone(D.pending_held(job, now=now))
        self.assertEqual(D.route(self.text, self.capmap, self.caps, job=job, now=now)["ranked"], [])
        write(now + 5000, status="done")
        self.assertIsNone(D.pending_held(job, now=now))
        self.assertTrue(D.route(self.text, self.capmap, self.caps, job=job, now=now)["ranked"])
        self.assertFalse(os.path.exists(lease))

    def test_caps_json_unchanged_phase_5(self):
        h = hashlib.sha256()
        with open(os.path.join(ROOT, "caps.json"), "rb") as f:
            h.update(f.read())
        self.assertTrue(h.hexdigest().startswith(CAPS_SHA_PREFIX))
        self.assertIsNone(D.HEALTH_COOLDOWN_S)

    def test_stale_repoll_keeps_first_failure_time(self):
        t0 = 1_700_000_000.0
        rec = HC.submit_async("async-fake", "stale-job", now=t0)
        deadline = rec["deadline"]
        self.assertEqual(deadline, t0 + HC.ASYNC_DEADLINE_MIN * 60)
        self.assertEqual(rec["status"], "PENDING")
        pend = HC.pending_path("stale-job")
        with open(pend, "rb") as f:
            raw = f.read()
        first = HC.poll_async("stale-job", now=deadline + 10)
        self.assertEqual(first["status"], "STALE")
        self.assertEqual(HC.read_health()["async-fake"], {"class": "STALE", "ts": deadline})
        with open(D.health_path(), "rb") as f:
            health_raw = f.read()
        second = HC.poll_async("stale-job", now=deadline + 500)
        self.assertEqual(second["status"], "STALE")
        self.assertEqual(HC.read_health()["async-fake"]["ts"], deadline)
        self.assertEqual(HC.read_health()["async-fake"]["class"], "STALE")
        with open(D.health_path(), "rb") as f:
            self.assertEqual(f.read(), health_raw)
        with open(pend, "rb") as f:
            self.assertEqual(f.read(), raw)
        later = deadline + 1000
        HC.record_health("async-fake", "done", now=later)
        self.assertEqual(HC.read_health()["async-fake"], {"class": "done", "ts": later})
        with open(D.health_path(), "rb") as f:
            done_raw = f.read()
        again = HC.poll_async("stale-job", now=deadline + 5000)
        self.assertEqual(again["status"], "STALE")
        self.assertEqual(HC.read_health()["async-fake"], {"class": "done", "ts": later})
        with open(D.health_path(), "rb") as f:
            self.assertEqual(f.read(), done_raw)
        with open(pend, "rb") as f:
            self.assertEqual(f.read(), raw)
        with open(pend) as f:
            disk = json.load(f)
        self.assertEqual(disk["status"], "PENDING")
        self.assertFalse(disk["stale"])

    def test_pin_does_not_walk(self):
        seen = []
        rc, out = self.go(self._returning("failed_retryable", seen), pin="grok-build")
        self.assertEqual(rc, 6, out["handoff"]["status"])
        self.assertEqual([h for h, _j, _p in seen], ["grok-build"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build"])
        self.assertTrue(out["handoff"]["pinned"])
        self.assertEqual(out["handoff"]["would_send_to"], "grok-build")
        self.assertGreater(len(out["route"]["ranked"]), 1)
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))
        self.assertNotIn("agy-gemini", [h for h, _j, _p in seen])

    def test_partial_receipt_holds(self):
        now = 5_000_000.0
        job = "partial-receipt-fl"
        receipt = os.path.join(self.tmp, "partial-fl.md")
        ok, _msg = D.lease_acquire(job, "grok-build", now=now)
        self.assertTrue(ok)
        self.assertEqual(D.lease_held(job, now=now)["acquired"], now)
        ok, _msg = D.lease_extend(job, "grok-build", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        with open(receipt, "wb") as f:
            f.write(b"")
        self.assertIsNotNone(D.lease_held(job, now=now))
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("working, no result line\n")
        self.assertGreater(os.path.getsize(receipt), 0)
        self.assertIsNotNone(D.lease_held(job, now=now))

    def test_prior_run_receipt_holds(self):
        now = 5_000_000.0
        job = "prior-receipt"
        receipt = os.path.join(self.tmp, "prior-receipt.md")
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("Result: done\n")
        os.utime(receipt, (now - 50, now - 50))
        ok, _msg = D.lease_acquire(job, "cursor", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "cursor", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        self.assertLess(os.path.getmtime(receipt), now)
        self.assertIsNotNone(D.lease_held(job, now=now))

    def test_oserror_receipt_holds(self):
        now = 5_000_000.0
        job = "oserror-receipt-fl"
        receipt = os.path.join(self.tmp, "oserror-fl.md")
        ok, _msg = D.lease_acquire(job, "agy-gemini", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "agy-gemini", now + 1000, receipt=receipt)
        self.assertTrue(ok)
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

    def test_complete_fresh_receipt_frees(self):
        now = 5_000_000.0
        job = "fresh-receipt"
        receipt = os.path.join(self.tmp, "fresh-receipt.md")
        ok, _msg = D.lease_acquire(job, "grok-build", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "grok-build", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("Result: done\n")
        self.assertGreaterEqual(os.path.getmtime(receipt), now)
        self.assertIsNone(D.lease_held(job, now=now))

    def test_expiry_still_frees(self):
        now = 5_000_000.0
        job = "expiry-receipt"
        receipt = os.path.join(self.tmp, "expiry-receipt.md")
        ok, _msg = D.lease_acquire(job, "async-fake", ttl=600, now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "async-fake", now + 600, receipt=receipt)
        self.assertTrue(ok)
        with open(receipt, "wb") as f:
            f.write(b"PAR")
        self.assertIsNotNone(D.lease_held(job, now=now + 100))
        self.assertIsNone(D.lease_held(job, now=now + 600))

    def test_lease_acquire_partial_receipt_holds(self):
        now = 5_000_000.0
        job = "acq-partial"
        receipt = os.path.join(self.tmp, "acq-partial.md")
        ok, _msg = D.lease_acquire(job, "grok-build", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "grok-build", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        with open(receipt, "wb") as f:
            f.write(b"")
        ok, msg = D.lease_acquire(job, "cursor", now=now + 1)
        self.assertFalse(ok)
        self.assertIn("REFUSED", msg)
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("working, no result line\n")
        self.assertGreater(os.path.getsize(receipt), 0)
        ok, msg = D.lease_acquire(job, "cursor", now=now + 1)
        self.assertFalse(ok, msg)
        self.assertEqual(D.lease_held(job, now=now + 1)["owner"], "grok-build")

    def test_lease_acquire_prior_run_receipt_holds(self):
        now = 5_000_000.0
        job = "acq-prior"
        receipt = os.path.join(self.tmp, "acq-prior.md")
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("Result: done\n")
        os.utime(receipt, (now - 50, now - 50))
        ok, _msg = D.lease_acquire(job, "cursor", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "cursor", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        self.assertLess(os.path.getmtime(receipt), now)
        ok, msg = D.lease_acquire(job, "agy-gemini", now=now + 1)
        self.assertFalse(ok, msg)
        self.assertEqual(D.lease_held(job, now=now + 1)["owner"], "cursor")

    def test_lease_acquire_oserror_receipt_holds(self):
        now = 5_000_000.0
        job = "acq-oserror"
        receipt = os.path.join(self.tmp, "acq-oserror.md")
        ok, _msg = D.lease_acquire(job, "agy-gemini", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "agy-gemini", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        real = os.path.exists

        def exists(path):
            if path == receipt:
                raise OSError("stat failed")
            return real(path)

        os.path.exists = exists
        try:
            ok, msg = D.lease_acquire(job, "async-fake", now=now + 1)
            self.assertFalse(ok, msg)
            self.assertIn("REFUSED", msg)
        finally:
            os.path.exists = real
        self.assertEqual(D.lease_held(job, now=now + 1)["owner"], "agy-gemini")

    def test_lease_acquire_complete_fresh_receipt_frees(self):
        now = 5_000_000.0
        job = "acq-fresh"
        receipt = os.path.join(self.tmp, "acq-fresh.md")
        ok, _msg = D.lease_acquire(job, "grok-build", now=now)
        self.assertTrue(ok)
        ok, _msg = D.lease_extend(job, "grok-build", now + 1000, receipt=receipt)
        self.assertTrue(ok)
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("Result: done\n")
        self.assertGreaterEqual(os.path.getmtime(receipt), now)
        ok, msg = D.lease_acquire(job, "cursor", now=now + 1)
        self.assertTrue(ok, msg)
        self.assertEqual(D.lease_held(job, now=now + 1)["owner"], "cursor")

    def test_pending_held_partial_receipt_holds(self):
        now = 5_000_000.0
        job = "pend-partial-b8"
        receipt = os.path.join(self.tmp, "pend-partial-b8.md")
        with open(receipt, "wb") as f:
            f.write(b"")
        self._pending(job, now + 1000, receipt=receipt, submitted=now - 10)
        self.assertIsNotNone(D.pending_held(job, now=now))
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("working, no result line\n")
        rec = D.pending_held(job, now=now)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["job"], job)
        self.assertIsNone(D.pending_held(job, now=now + 1000))

    def test_pending_held_prior_run_receipt_holds(self):
        now = 5_000_000.0
        job = "pend-prior-b8"
        receipt = os.path.join(self.tmp, "pend-prior-b8.md")
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("Result: done\n")
        os.utime(receipt, (now - 50, now - 50))
        self._pending(job, now + 1000, receipt=receipt, submitted=now)
        self.assertLess(os.path.getmtime(receipt), now)
        rec = D.pending_held(job, now=now)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["job"], job)

    def test_pending_held_oserror_receipt_holds(self):
        now = 5_000_000.0
        job = "pend-oserror-b8"
        receipt = os.path.join(self.tmp, "pend-oserror-b8.md")
        self._pending(job, now + 1000, receipt=receipt, submitted=now - 1)
        real = os.path.exists

        def exists(path):
            if path == receipt:
                raise OSError("stat failed")
            return real(path)

        os.path.exists = exists
        try:
            rec = D.pending_held(job, now=now)
            self.assertIsNotNone(rec)
            self.assertEqual(rec["job"], job)
        finally:
            os.path.exists = real

    def test_pending_held_complete_fresh_receipt_frees(self):
        now = 5_000_000.0
        job = "pend-fresh-b8"
        receipt = os.path.join(self.tmp, "pend-fresh-b8.md")
        with open(receipt, "w", encoding="utf-8") as f:
            f.write("Result: done\n")
        self._pending(job, now + 5000, receipt=receipt)
        self.assertIsNotNone(D.pending_held(job, now=now))
        self._pending(job, now + 5000, receipt=receipt, submitted=now - 5)
        self.assertGreaterEqual(os.path.getmtime(receipt), now - 5)
        self.assertIsNone(D.pending_held(job, now=now))


if __name__ == "__main__":
    unittest.main(verbosity=2)
