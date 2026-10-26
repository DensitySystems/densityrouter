"""Live wiring: live caller behind DISPATCH_LIVE (default OFF). FAKE harness only:
real harness_call.call is monkeypatched to fail loudly if touched. DISPATCH_LIVE=1 appears only in
per-test env dicts passed to live.run(); os.environ is never set. Temp ledger/runs/state/KILL only."""
import json, os, shutil, subprocess, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, live as LV, harness_call as H
ON = {"DISPATCH_LIVE": "1"}

class Live(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        self.t = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_KILL", "DISPATCH_STATE")}
        os.environ["DISPATCH_KILL"] = D.KILL = os.path.join(self.t, "KILL")
        os.environ["DISPATCH_STATE"] = os.path.join(self.t, "state")
        self.pk = os.path.join(self.t, "PACKET-WIRE-FAKE-r1.md")
        with open(self.pk, "w") as f:
            f.write("# PACKET: fake\n\n## Objective\nFix a script in the scratch dir.\n")
        s12 = D.sha256f(self.pk)[:12]
        self.ledger = os.path.join(self.t, "ledger.md"); self.runs = os.path.join(self.t, "RUNS.tsv")
        with open(self.ledger, "w") as f: f.write(f"| 1 | GO | PACKET-WIRE-FAKE-r1.md {s12} |\n")
        with open(self.runs, "w") as f: f.write(f"x\t{self.pk}\n")
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
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        shutil.rmtree(self.t, ignore_errors=True)
    def fake(self, h, job=None, **k):
        self.calls.append((h, job)); return {"harness": h, "status": "done", "stdout": "OK"}
    def go(self, env, **kw):
        env = dict(env); env.setdefault("DISPATCH_STATE", os.environ["DISPATCH_STATE"])  # temp state only
        return LV.run(self.pk, self.ledger, self.runs, self.capmap, self.caps, env=env, **kw)

    def test_flag_off_zero_calls(self):
        for env in ({}, {"DISPATCH_LIVE": ""}, {"DISPATCH_LIVE": "0"}, {"DISPATCH_LIVE": "true"}, {"DISPATCH_LIVE": " 1"}, {"DISPATCH_LIVE": "yes"}):
            rc, out = self.go(env, call_fn=self.fake)
            self.assertEqual((rc, out["mode"]), (0, "offline"), env)
            self.assertEqual(out["handoff"]["status"], "WOULD DISPATCH (dry-run, nothing sent)")
        self.assertEqual(self.calls, [])
    def test_flag_off_matches_dry_run(self):
        ok, ref, _ = D.dry_run(self.pk, self.ledger, self.runs, self.capmap, self.caps)
        rc, out = self.go({}); out.pop("mode"); self.assertEqual(out, ref)
    def test_flag_on_fake_exactly_one_routed_call(self):
        rc, out = self.go(ON, call_fn=self.fake)
        self.assertEqual(rc, 0, json.dumps(out)); self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], ("grok-build", out["handoff"]["job"]))
        self.assertIn("ROUTED LIVE -> grok-build", out["handoff"]["status"])
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))  # lease released after call
    def test_flag_on_default_path_uses_patched_call(self):
        H.call = self.fake  # default wiring, still fake; the grok persistent session is faked off (never a real CLI)
        rc, out = self.go(ON, session_fn=lambda cwd: None); self.assertEqual((rc, len(self.calls)), (0, 1))
    def test_kill_switch_blocks(self):
        open(D.KILL, "w").close()
        with self.assertRaises(SystemExit) as e: self.go(ON, call_fn=self.fake)
        self.assertEqual(e.exception.code, 3); self.assertEqual(self.calls, [])
    def test_hold_when_no_go(self):
        open(self.ledger, "w").close()
        rc, out = self.go(ON, call_fn=self.fake); self.assertEqual((rc, self.calls), (2, []))
    def test_lease_held_by_other_holds(self):
        ok, ref, _ = D.dry_run(self.pk, self.ledger, self.runs, self.capmap, self.caps)
        D.lease_acquire(ref["handoff"]["job"], "other-owner")
        rc, out = self.go(ON, call_fn=self.fake); self.assertEqual((rc, self.calls), (2, []))
    def test_redaction_on_call_output(self):
        def leaky(h, job=None, **k): self.calls.append(h); return {"status": "done", "stdout": "Bearer abcdefghijklmnop"}
        rc, out = self.go(ON, call_fn=leaky)
        self.assertNotIn("abcdefghijklmnop", json.dumps(out)); self.assertGreaterEqual(out["redaction_hits"], 1)
    def test_refused_result_not_done(self):
        def capped(h, job=None, **k): self.calls.append(h); return {"harness": h, "status": "REFUSED", "reason": "forbidden flag"}
        rc, out = self.go(ON, call_fn=capped); self.assertEqual(rc, 6)
    # state/LIVE file flag, temp DISPATCH_STATE only
    def _flagfile(self): return os.path.join(os.environ["DISPATCH_STATE"], "LIVE")
    def test_no_file_no_env_zero_calls(self):
        self.assertFalse(os.path.exists(self._flagfile()))
        rc, out = self.go({"DISPATCH_STATE": os.environ["DISPATCH_STATE"]}, call_fn=self.fake)
        self.assertEqual((out["mode"], self.calls), ("offline", []))
    def test_file_present_exactly_one_call(self):
        os.makedirs(os.environ["DISPATCH_STATE"], exist_ok=True); open(self._flagfile(), "w").close()
        rc, out = self.go({"DISPATCH_STATE": os.environ["DISPATCH_STATE"]}, call_fn=self.fake)
        self.assertEqual((rc, out["mode"], len(self.calls)), (0, "live", 1))
    def test_file_removed_is_rollback(self):
        os.makedirs(os.environ["DISPATCH_STATE"], exist_ok=True); open(self._flagfile(), "w").close(); os.remove(self._flagfile())
        rc, out = self.go({"DISPATCH_STATE": os.environ["DISPATCH_STATE"]}, call_fn=self.fake)
        self.assertEqual((out["mode"], self.calls), ("offline", []))
    def test_kill_beats_file(self):
        os.makedirs(os.environ["DISPATCH_STATE"], exist_ok=True); open(self._flagfile(), "w").close(); open(D.KILL, "w").close()
        with self.assertRaises(SystemExit) as e: self.go({"DISPATCH_STATE": os.environ["DISPATCH_STATE"]}, call_fn=self.fake)
        self.assertEqual((e.exception.code, self.calls), (3, []))
    def test_default_live_file_is_dispatcher_state(self):
        self.assertEqual(LV.live_file({}), os.path.join(ROOT, "state", "LIVE"))

    def test_rollback_off_removes_live(self):
        state = os.environ["DISPATCH_STATE"]
        os.makedirs(state, exist_ok=True)
        env = {"DISPATCH_STATE": state}
        path = LV.live_file(env)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        self.assertTrue(LV.live_enabled(env))
        msg = LV.rollback_off(env)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(LV.live_enabled(env))
        self.assertIn("removed", msg)
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")

    def test_rollback_off_idempotent_when_absent(self):
        state = os.environ["DISPATCH_STATE"]
        os.makedirs(state, exist_ok=True)
        env = {"DISPATCH_STATE": state}
        path = LV.live_file(env)
        self.assertFalse(os.path.exists(path))
        msg1 = LV.rollback_off(env)
        msg2 = LV.rollback_off(env)
        self.assertIn("not present", msg1)
        self.assertIn("not present", msg2)
        self.assertNotIn("removed", msg1)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(LV.live_enabled(env))
        flagged = {"DISPATCH_STATE": state, "DISPATCH_LIVE": "1"}
        warned = LV.rollback_off(flagged)
        self.assertIn("must be unset", warned)
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")

    def test_cli_off_removes_live(self):
        state = os.path.join(self.t, "cli-off-state")
        os.makedirs(state)
        live = os.path.join(state, "LIVE")
        with open(live, "w", encoding="utf-8") as f:
            f.write("")
        env = os.environ.copy()
        env["DISPATCH_STATE"] = state
        env["DISPATCH_KILL"] = os.path.join(self.t, "cli-off-kill")
        env.pop("DISPATCH_LIVE", None)
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "live.py"), "--off"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(live))
        self.assertIn("removed", proc.stdout)
        self.assertFalse(LV.live_enabled({"DISPATCH_STATE": state}))
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")

if __name__ == "__main__": unittest.main(verbosity=1)
