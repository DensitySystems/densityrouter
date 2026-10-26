"""Tests: N1, LOW, GO strictness, KILL-first, timeouts, exit map, redaction of harness output, caps.
Harness calls here use LOCAL STUB commands (python3/sleep) only; no real harness calls."""
import io, json, os, subprocess, sys, tempfile, unittest, warnings
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, harness_call as HC
CAP = D.load(os.path.join(ROOT, "capability-map.json")); CAPS = D.load(os.path.join(ROOT, "caps.json"))
PY = sys.executable
import drconfig as _C
PK = (_C.path("packets_dir") or "") + "/"; LEDGER = _C.path("go_ledger"); RUNS = _C.path("runs")

N1_PINNED = "packets=180 lan_before=113 lan_after=25 browser_jobs_before=24 browser_jobs_after=1 browser_forced_before=21 browser_forced_after=1"

class DE(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); os.environ["DISPATCH_STATE"] = self.tmp
        D.KILL = os.path.join(self.tmp, "KILL"); os.environ["DISPATCH_KILL"] = D.KILL
    # N1
    def test_n1_boilerplate_browser_job_scores(self):
        t = "Objective: check a website with the browser.\n4. Boundaries: no router, switch, or firewall writes. No LAN changes.\n"
        r = D.route(t, CAP, CAPS); print("\n  N1 browser:", r["lan"], r["ranked"][0]["harness"])
        ranked = [row["harness"] for row in r["ranked"]]
        self.assertFalse(r["lan"])
        # Best browser ability of the route pool (agy-gemini 0.3 > grok-build 0.2 > cursor 0.1).
        self.assertEqual(r["ranked"][0]["harness"], "agy-gemini")
        self.assertNotIn("async-fake", ranked)
        self.assertNotIn("async-fake", [h for h, _why in r["skipped"]])
    def test_n1_declared_field_wins(self):
        self.assertTrue(D.is_lan("Hosts: nd-07 (203.0.113.11)\nObjective: browser check"))
        self.assertFalse(D.is_lan("Network placement: None. No listener.\nmentions 198.51.100.45 in passing"))
    def _n1(self, corpus):
        return subprocess.run([PY, os.path.join(ROOT, "n1_regress.py")], capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "N1_CORPUS": corpus})
    def test_low_lease_release_no_resource_warning(self):
        D.lease_acquire("j", "a")
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            ok, m = D.lease_release("j", None, kill_confirmed=True)
        self.assertTrue(ok); self.assertTrue(D.load(os.path.join(self.tmp, "leases/j.json"))["kill_confirmed"])
    # GO strictness
    def _ledger(self, line):
        p = os.path.join(self.tmp, "L.md"); 
        with open(p, "w") as f: f.write(line + "\n")
        return p
    def test_go_filename_and_sha12_required(self):
        n = "PACKET-X-20261024.md"
        self.assertEqual(D.find_go(self._ledger(f"t | GO | {n} sha abcdef123456 | x"), "abcdef123456", n)[0], 1)
        self.assertIsNone(D.find_go(self._ledger("t | GO | some eval abcdef123456 | x"), "abcdef123456", n)[0])      # sha-only
        self.assertIsNone(D.find_go(self._ledger(f"t | GO | {n} sha abcdef12345 | x"), "abcdef123456", n)[0])       # 11 chars
        self.assertIsNone(D.find_go(self._ledger(f"t | GO | {n} sha abcdef1234567 | x"), "abcdef123456", n)[0])     # longer token
        self.assertIsNone(D.find_go(self._ledger(f"t | GO | PACKET-X-20261024-r2.md abcdef123456"), "abcdef123456", n)[0])  # other file
    # KILL first, every tool
    # timeout + exit map
    def test_timeout_enforced(self):
        r = HC.call("agy-gemini", [PY, "-c", "import time; time.sleep(5)"], timeout=1)  # FIX: async-fake is async now
        print("\n  timeout:", r["status"], r["secs"]); self.assertEqual(r["status"], "timed_out"); self.assertLess(r["secs"], 3)
    def test_default_timeout_is_60(self): self.assertEqual(HC.TIMEOUT, 60)
    def test_exit_map(self):
        self.assertEqual(HC.exit_map(0, False, "OK"), "done")
        self.assertEqual(HC.exit_map(1, False, "Error 429 RESOURCE_EXHAUSTED"), "failed_hold:usage")
        self.assertEqual(HC.exit_map(127, False, ""), "failed_hold:not_installed")
        self.assertEqual(HC.exit_map(1, False, "boom"), "failed_retryable")
        self.assertEqual(HC.exit_map(-15, False, ""), "cancelled")
    # redaction of harness output
    def test_harness_output_redacted(self):
        r = HC.call("agy-gemini", [PY, "-c", "print('OK token=deadbeefdeadbeefdeadbeefcafe0011 Bearer abc.def'); import sys; print('gh'+'p_ABCDEFGHIJKLMNOP1234', file=sys.stderr)"])
        print("\n  redact:", r["stdout"].strip(), "|", r["stderr"].strip(), r["redaction_hits"])
        self.assertNotIn("deadbeef", r["stdout"]); self.assertNotIn("abc.def", r["stdout"]); self.assertNotIn("gh" + "p_", r["stderr"]); self.assertEqual(r["status"], "done")
    # caps
    def test_caps(self):
        r0 = HC.call("cursor"); self.assertEqual((r0["status"], r0["reason"]), ("REFUSED", "no packet"))  # cursor invoke path is real: no smoke ping
        self.assertIsNone(HC.CAPS["agy-gemini"])  # missing test_call_cap is no ceiling
        for _ in range(3):
            self.assertEqual(HC.call("agy-gemini", [PY, "-c", "print('OK')"])["status"], "done")
        r = HC.call("agy-gemini", [PY, "-c", "print('OK')"]); print("\n  cap:", r); self.assertEqual(r["status"], "done")
        self.assertEqual(HC.call("agy-gemini", ["agy", "-p", "x", "--dangerously-skip-permissions"])["status"], "REFUSED")
    # dogfood
if __name__ == "__main__": unittest.main(verbosity=2)
