import json, os, subprocess, sys, tempfile, time, threading, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
sys.path.insert(0, ROOT); import dispatcher as D
CAP = D.load(os.path.join(ROOT, "capability-map.json")); CAPS = D.load(os.path.join(ROOT, "caps.json"))
import drconfig as _C
PK = (_C.path("packets_dir") or "") + "/"; LEDGER = _C.path("go_ledger"); RUNS = _C.path("runs")
PAST = ["PACKET-EXAMPLE-A.md", "PACKET-EXAMPLE-B.md", "PACKET-EXAMPLE-C.md"]

class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); os.environ["DISPATCH_STATE"] = self.tmp
        D.KILL = os.path.join(self.tmp, "KILL")
    # M1
    def test_m1_concurrent_reroute_refused(self):
        ok, m = D.lease_acquire("job1", "grok-build"); self.assertTrue(ok)
        ok2, m2 = D.lease_acquire("job1", "agy-gemini"); print("\n  M1:", m2); self.assertFalse(ok2)
        self.assertEqual(D.route("code fix", CAP, CAPS, job="job1")["ranked"], [])
    def test_m1_race_one_winner(self):
        res = []
        ts = [threading.Thread(target=lambda s=s: res.append(D.lease_acquire("job2", s)[0])) for s in ("a","b","c","d")]
        [t.start() for t in ts]; [t.join() for t in ts]
        print("\n  M1 race winners:", res.count(True)); self.assertEqual(res.count(True), 1)
    def test_m1_reroute_after_release_expiry_kill(self):
        D.lease_acquire("j3", "a"); D.lease_release("j3", "a"); self.assertTrue(D.lease_acquire("j3", "b")[0])
        D.lease_acquire("j4", "a", ttl=-1); self.assertTrue(D.lease_acquire("j4", "b")[0])
        D.lease_acquire("j5", "a"); D.lease_release("j5", None, kill_confirmed=True); self.assertTrue(D.lease_acquire("j5", "b")[0])
    # M2
    def test_m2_sha_in_known_field_passes(self):
        s64 = "4b0e58e34dccf717e80b1f20be076cf23a0f20bea03715190743e78a6bcdfa2b"
        out, h = D.redact({"packet_sha": s64, "sha": "abcdef123456", "commit": "0123456789ab"}); print("\n  M2 pass:", out, h)
        self.assertEqual(h, 0); self.assertEqual(out["packet_sha"], s64)
    def test_m2_hex_in_free_text_redacted(self):
        out, h = D.redact({"note": "token deadbeefdeadbeefdeadbeefcafebabe0011 here", "sha": "nothex-but-long-value-zzzz"})
        print("\n  M2 redact:", out, h); self.assertIn("[REDACTED]", out["note"]); self.assertGreaterEqual(h, 1)
    def test_m2_sha_shaped_in_unknown_field_redacted(self):
        out, h = D.redact({"comment": "4b0e58e34dccf717e80b1f20be076cf23a0f20bea03715190743e78a6bcdfa2b"}); self.assertEqual(h, 1)
    def test_m2_bearer_redacted(self):
        out, h = D.redact({"x": "Authorization: Bearer abc.def-ghi"}); self.assertNotIn("abc.def", out["x"])
    # M3
    def test_m3_score_beats_fallback(self):
        r = D.route("research the topic", CAP, CAPS); top = r["ranked"][0]["harness"]
        print("\n  M3 research top:", top, r["decided_by"]); self.assertEqual(top, "agy-gemini")  # fallback would say grok-build
    def test_m3_hard_rule_beats_score(self):
        caps = json.loads(json.dumps(CAPS)); caps["cursor"].update(enabled=True, daily_cap=10)
        r = D.route("repo-wide refactor", CAP, caps); self.assertEqual(r["ranked"][0]["harness"], "cursor")
        r = D.route("repo-wide refactor", CAP, CAPS); self.assertEqual(r["ranked"][0]["harness"], "cursor")
        r = D.route("research on nd-07 (203.0.113.11)", CAP, CAPS)  # N1: concrete LAN target, not bare words; self.assertEqual([x["harness"] for x in r["ranked"]], ["grok-build"])
    def test_m3_tie_uses_fallback(self):
        cm = {h: {"code_edit": 0.5} for h in D.FALLBACK}; r = D.route("code", cm, CAPS)
        print("\n  M3 tie:", r["ranked"][0]["harness"], r["decided_by"]); self.assertEqual(r["ranked"][0]["harness"], "grok-build"); self.assertIn("fallback", r["decided_by"])
    def test_m3_unknown_uses_fallback(self):
        cm = {h: {} for h in D.FALLBACK}; r = D.route("code", cm, CAPS); self.assertIn("unknown", r["decided_by"])
    def test_cap_over_hold(self):
        # No cap gate: missing, null, 0, and a positive daily_cap all stay ranked.
        caps = json.loads(json.dumps(CAPS))
        caps["grok-build"]["daily_cap"] = 1; caps["grok-build"]["used_today"] = 50
        caps["cursor"]["daily_cap"] = 0; caps["cursor"]["used_today"] = 9
        caps["agy-gemini"]["daily_cap"] = None; caps["agy-gemini"]["used_today"] = 9
        
        r = D.route("code", CAP, caps)
        ranked = [x["harness"] for x in r["ranked"]]
        self.assertEqual(sorted(ranked), sorted(D.FALLBACK))
        self.assertIn("grok-build", ranked)
        self.assertFalse(any("cap" in why for _h, why in r["skipped"]))
    # L2
    def test_usage_fresh_snapshot(self):
        r = D.route("code", CAP, CAPS, udir=os.path.join(HERE, "fixtures/usage"), now=time.time())
        self.assertFalse(r["phase1"])
    # L3
    def test_l3_adapters_have_owner_and_off(self):
        for h, a in D.ADAPTERS.items(): self.assertTrue(a["owner"] and a["off"])
    # C: dry-run on 5 past packets
if __name__ == "__main__": unittest.main(verbosity=2)
