"""The packet approval gate (GO ledger line and RUNS row) is gone; the other guards still refuse.
Every test here runs with NO ledger and NO RUNS file anywhere. Fake harness only: the real call is replaced
with a function that fails loudly if touched. Temp state/KILL only; DISPATCH_LIVE appears only in per-test env dicts."""
import json, os, shutil, subprocess, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, live as LV, harness_call as H
import drconfig as C
ON = {"DISPATCH_LIVE": "1"}
SECRET = "sk" + "-testkey1234567890"
PLAIN = "# PACKET: plain\n\nObjective: Fix a script in the scratch dir.\n"
LAN = "Objective: Research the queue notes.\nPlacement: LAN\n"

class GateDropped(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        self.t = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_KILL", "DISPATCH_STATE")}
        os.environ["DISPATCH_KILL"] = D.KILL = os.path.join(self.t, "KILL")
        os.environ["DISPATCH_STATE"] = os.path.join(self.t, "state")
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = {"grok-build": {"enabled": True}, "cursor": {"enabled": False}, "agy-gemini": {"enabled": False}, "async-fake": {"enabled": False}}
        self.calls = []
        self._real = H.call
        def boom(*a, **k): raise AssertionError("REAL harness touched")
        H.call = boom
    def tearDown(self):
        H.call = self._real
        for k, v in self._env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        D.KILL = os.environ.get("DISPATCH_KILL", os.path.join(D.HERE, "KILL"))
        shutil.rmtree(self.t, ignore_errors=True)
    def fake(self, h, job=None, **k):
        self.calls.append((h, job)); return {"harness": h, "status": "done", "stdout": "OK"}
    def packet(self, text=PLAIN, name="PACKET-GATE-r1.md"):
        p = os.path.join(self.t, name)
        with open(p, "w") as f: f.write(text)
        return p
    def live(self, pk, **kw):
        env = dict(ON, DISPATCH_STATE=os.environ["DISPATCH_STATE"])
        return LV.run(pk, self.capmap, self.caps, env=env, call_fn=self.fake, **kw)
    def cli(self, *args):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}; env.pop("DISPATCH_LIVE", None)
        return subprocess.run([sys.executable, os.path.join(ROOT, "dispatcher.py"), *args], capture_output=True, text=True, env=env)

    # ---- the gate is gone
    def test_no_gate_functions(self):
        self.assertFalse(hasattr(D, "find_go")); self.assertFalse(hasattr(D, "find_runs"))
    def test_no_ledger_or_runs_config_keys(self):
        self.assertIsNone(C.path("go_ledger")); self.assertIsNone(C.path("runs"))
    def test_dry_run_routes_without_ledger_or_runs(self):
        ok, out, _ = D.dry_run(self.packet(), self.capmap, self.caps)
        self.assertTrue(ok); self.assertEqual(out["handoff"]["status"], "WOULD DISPATCH (dry-run, nothing sent)")
        self.assertNotIn("go_line", out["handoff"]); self.assertNotIn("runs_row_found", out)
    def test_live_routes_without_ledger_or_runs(self):
        rc, out = self.live(self.packet())
        self.assertEqual((rc, len(self.calls)), (0, 1)); self.assertEqual(self.calls[0][0], "grok-build")
    def test_cli_dry_run_exit_0_with_no_flags(self):
        r = self.cli("dispatch", "--dry-run", self.packet())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn("WOULD DISPATCH", r.stdout)
    def test_cli_old_flags_accepted_and_ignored(self):
        r = self.cli("dispatch", "--dry-run", self.packet(), "--ledger", os.path.join(self.t, "none.md"), "--runs", os.path.join(self.t, "none.tsv"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # ---- the kept guards still refuse (no ledger/RUNS present in any of these)
    def test_kill_file_still_refuses(self):
        pk = self.packet(); open(D.KILL, "w").close()
        with self.assertRaises(SystemExit) as e: self.live(pk)
        self.assertEqual(e.exception.code, 3); self.assertEqual(self.calls, [])
        with self.assertRaises(SystemExit) as e: D.dry_run(pk, self.capmap, self.caps)
        self.assertEqual(e.exception.code, 3)
        r = self.cli("dispatch", "--dry-run", pk)
        self.assertEqual(r.returncode, 3)
    def test_lease_still_refuses(self):
        pk = self.packet()
        ok, ref, _ = D.dry_run(pk, self.capmap, self.caps)
        D.lease_acquire(ref["handoff"]["job"], "other-owner")
        rc, out = self.live(pk)
        self.assertEqual((rc, self.calls), (2, []))
        self.assertEqual(out["route"]["decided_by"], "hard: lease held -> no reroute")
    def test_lan_rule_still_refuses(self):
        pk = self.packet(LAN, "PACKET-GATE-LAN-r1.md")
        rc, out = self.live(pk, pin="cursor")
        self.assertTrue(out["route"]["lan"]); self.assertEqual((rc, self.calls), (7, []))
        self.assertIn("LAN work -> grok-build only", out["handoff"]["status"])
        rc, out = self.live(pk)  # unpinned: LAN work may only rank the configured LAN harness
        self.assertEqual({r["harness"] for r in out["route"]["ranked"]}, {C.setting("lan", "harness")})
    def test_secret_refusal_still_refuses(self):
        pk = self.packet(PLAIN + "Token: %s\n" % SECRET, "PACKET-GATE-SECRET-r1.md")
        rc, out = self.live(pk)
        self.assertEqual((rc, self.calls), (8, []))
        self.assertGreater(out["packet_redaction_hits"], 0); self.assertNotIn(SECRET, json.dumps(out))

if __name__ == "__main__": unittest.main()
