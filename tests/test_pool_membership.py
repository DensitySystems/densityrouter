"""Phase 6: cursor is scored. enabled is not a skip. A missing, null, or 0 cap is no ceiling.
Stdlib unittest. Temp DISPATCH_STATE only. subprocess.run is patched to raise. No network, no real harness.
Run: python3 tests/test_pool_membership.py
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
from datetime import datetime, timezone
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, harness_call as HC

CAPS_SHA_PREFIX = "02a81b2eb0a8"
THREE = ["grok-build", "cursor", "agy-gemini"]
CODE = "Objective: fix the script.\nPlacement: local\n"
REFACTOR = "Objective: repo-wide refactor of the modules.\nPlacement: local\n"
LAN = "Objective: fix the script.\nPlacement: LAN\n"


class PoolMembership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL")}
        os.environ["DISPATCH_STATE"] = self.tmp
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL")
        os.environ["DISPATCH_KILL"] = D.KILL
        self._subprocess_run = subprocess.run
        self._hc_run = HC.subprocess.run

        def boom(*a, **k):
            raise AssertionError("subprocess.run")

        HC.subprocess.run = boom
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        with open(os.path.join(ROOT, "caps.json"), "rb") as f:
            self._caps_raw = f.read()
        self.assertIsNone(D.HEALTH_COOLDOWN_S)
        self.assertEqual(HC.INVOKE["cursor"][1:], ["-p", "--trust", "--output-format", "text", "--", HC.PROMPT]); self.assertTrue(os.path.isabs(HC.INVOKE["cursor"][0]))

    def tearDown(self):
        HC.subprocess.run = self._hc_run
        subprocess.run = self._subprocess_run
        D.KILL = self._kill
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        with open(os.path.join(ROOT, "caps.json"), "rb") as f:
            self.assertEqual(f.read(), self._caps_raw)
        self.assertIsNone(D.HEALTH_COOLDOWN_S)

    def _temp_caps(self):
        dest = os.path.join(self.tmp, "caps.json")
        with open(dest, "wb") as f:
            f.write(self._caps_raw)
        return D.load(dest)

    def _names(self, result):
        return [row["harness"] for row in result["ranked"]] + [h for h, _why in result["skipped"]]

    def _equal_usage(self, when):
        udir = os.path.join(self.tmp, "usage")
        os.makedirs(udir)
        for h in D.FALLBACK:
            snap = {
                "harness": h,
                "remaining_pct": 40,
                "window_len_h": 168,
                "reset_at": "2026-11-01T18:00:00+00:00",
                "burn_pct_per_h": 0,
                "read_at": when.isoformat(),
            }
            with open(os.path.join(udir, h + ".json"), "w") as f:
                json.dump(snap, f)
        return udir

    def test_cursor_scored_when_enabled_false(self):
        caps = self._temp_caps()
        self.assertIs(caps["cursor"]["enabled"], True)
        self.assertNotIn("daily_cap", caps["cursor"])
        caps["cursor"]["enabled"] = False
        self.assertIs(caps["cursor"]["enabled"], False)
        when = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
        udir = self._equal_usage(when)
        r = D.route(CODE, self.capmap, caps, udir, now=when.timestamp())
        self.assertEqual(r["task_type"], "code_edit")
        self.assertFalse(r["lan"])
        ranked = [row["harness"] for row in r["ranked"]]
        skipped = [h for h, _why in r["skipped"]]
        self.assertIn("cursor", ranked)
        self.assertNotIn("cursor", skipped)
        self.assertEqual(len({row["U"] for row in r["ranked"]}), 1)
        cursor = next(row for row in r["ranked"] if row["harness"] == "cursor")
        grok = next(row for row in r["ranked"] if row["harness"] == "grok-build")
        self.assertEqual(cursor["A"], grok["A"])
        self.assertEqual(cursor["score"], grok["score"])
        self.assertGreater(cursor["score"], 0)

    def test_zero_daily_cap_is_not_a_ceiling(self):
        # missing, null, and 0 are no ceiling.
        caps = {
            "grok-build": {"enabled": True, "used_today": 40},
            "cursor": {"enabled": False, "daily_cap": None, "used_today": 30},
            "agy-gemini": {"enabled": True, "daily_cap": 0, "used_today": 30},
            "async-fake": {"enabled": True, "daily_cap": 0, "used_today": 0},
        }
        r = D.route(CODE, self.capmap, caps)
        ranked = [row["harness"] for row in r["ranked"]]
        self.assertEqual(sorted(ranked), sorted(THREE))
        self.assertNotIn("async-fake", ranked)
        self.assertEqual(r["skipped"], [])
        self.assertNotIn("cursor", [h for h, _why in r["skipped"]])
        self.assertIn("cursor", ranked)
        self.assertIn("agy-gemini", ranked)
        self.assertIn("grok-build", ranked)

    def test_positive_daily_cap_still_ceilings(self):
        # route() has no cap gate. A positive daily_cap does not skip.
        caps = {
            "grok-build": {"enabled": True, "daily_cap": 50, "used_today": 50},
            "cursor": {"enabled": False, "daily_cap": 0, "used_today": 0},
            "agy-gemini": {"enabled": True, "daily_cap": 30, "used_today": 0},
            "async-fake": {"enabled": True, "daily_cap": 30, "used_today": 99},
        }
        r = D.route(CODE, self.capmap, caps)
        ranked = [row["harness"] for row in r["ranked"]]
        self.assertNotIn(("grok-build", "hard: over daily cap -> failed_hold"), r["skipped"])
        self.assertIn("grok-build", ranked)
        self.assertIn("cursor", ranked)
        self.assertNotIn("async-fake", ranked)
        self.assertNotIn("async-fake", [h for h, _why in r["skipped"]])
        self.assertFalse(any("cap" in why for _h, why in r["skipped"]))
        with open(os.path.join(ROOT, "caps.json"), "rb") as f:
            self.assertEqual(f.read(), self._caps_raw)

    def test_cursor_test_cap_zero_is_not_blocked(self):
        pkt = os.path.join(self.tmp, "PACKET-CURSOR.md")
        with open(pkt, "w", encoding="utf-8") as f:
            f.write(CODE)
        seen = []
        real = HC.build_argv

        def spy(h, text):
            seen.append((h, text))
            return real(h, text)

        HC.build_argv = spy
        fake = os.path.join(self.tmp, "cursor-agent"); open(fake, "w").close()  # fake CLI
        ran = []
        class P: returncode = 0; stdout = "OK"; stderr = ""
        boom_run = HC.subprocess.run; HC.subprocess.run = lambda argv, **k: (ran.append(list(argv)), P())[1]
        old0 = HC.INVOKE["cursor"][0]; HC.INVOKE["cursor"][0] = fake
        try:
            got = HC.call("cursor", packet=pkt)
        finally:
            HC.build_argv = real; HC.subprocess.run = boom_run; HC.INVOKE["cursor"][0] = old0
        self.assertEqual(got["status"], "done")
        self.assertNotEqual(got["status"], "BLOCKED")
        self.assertEqual(got["argv0"], "cursor-agent")
        self.assertEqual(ran, [[fake, "-p", "--trust", "--output-format", "text", "--", CODE]])
        self.assertEqual(seen, [("cursor", CODE)])
        self.assertFalse(HC.CAPS["cursor"])  # no cap = no ceiling

    def test_pool_is_exactly_three(self):
        caps = self._temp_caps()
        plain = D.route(CODE, self.capmap, caps)
        self.assertEqual(sorted(self._names(plain)), sorted(THREE))
        self.assertEqual(len(self._names(plain)), 3)
        self.assertEqual(len(set(self._names(plain))), 3)
        self.assertEqual(plain["skipped"], [])
        self.assertNotIn("async-fake", self._names(plain))
        lan = D.route(LAN, self.capmap, caps)
        self.assertEqual(sorted(self._names(lan)), sorted(THREE))
        self.assertEqual([row["harness"] for row in lan["ranked"]], ["grok-build"])
        self.assertNotIn("async-fake", self._names(lan))
        self.assertEqual(list(D.FALLBACK), THREE)
        self.assertNotIn("l28", D.ADAPTERS["cursor"]["off"])
        self.assertNotIn("Cursor off", D.__doc__)
        self.assertNotIn("l28", D.__doc__)
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("Cursor off", readme)
        self.assertNotIn("l28", readme)

    def test_async_fake_not_a_route_candidate(self):
        self.assertIn("async-fake", D.ROUTE_EXCLUDED)
        self.assertNotIn("async-fake", D.FALLBACK)
        self.assertEqual(list(D.FALLBACK), THREE)
        caps = self._temp_caps()
        for text in (CODE, REFACTOR, LAN, "Objective: check a website with the browser.\n"):
            r = D.route(text, self.capmap, caps)
            self.assertNotIn("async-fake", self._names(r))
            self.assertNotIn("async-fake", [row["harness"] for row in r["ranked"]])
            self.assertNotIn("async-fake", [h for h, _why in r["skipped"]])

    def test_no_local_tier_key(self):
        caps = self._temp_caps()
        for mapping in (self.capmap, caps, D.ADAPTERS):
            self.assertNotIn("local", mapping)
            for key in mapping:
                self.assertNotIn("local", key)
        for h in D.FALLBACK:
            self.assertNotIn("local", h)
        r = D.route(CODE, self.capmap, caps)
        for name in self._names(r):
            self.assertNotEqual(name, "local")
            self.assertNotIn("local", name)
        self.assertNotIn("local", self._names(r))

    def test_equal_u_refactor_picks_cursor(self):
        caps = self._temp_caps()
        self.assertIs(caps["cursor"]["enabled"], True)
        self.assertNotIn("daily_cap", caps["cursor"])
        for h in D.FALLBACK:
            u, state = D.usage_for(h, None)
            self.assertEqual((u, state), (D.U_DEFAULT, "unknown"))
        r = D.route(REFACTOR, self.capmap, caps)
        self.assertEqual(r["task_type"], "refactor")
        self.assertFalse(r["lan"])
        self.assertTrue(r["phase1"])
        self.assertEqual(len({row["U"] for row in r["ranked"]}), 1)
        self.assertEqual(r["ranked"][0]["harness"], "cursor")
        self.assertEqual(r["ranked"][1]["harness"], "grok-build")
        self.assertEqual(r["decided_by"], "score")
        self.assertGreater(r["ranked"][0]["score"], r["ranked"][1]["score"])
        self.assertEqual(r["ranked"][0]["A"], self.capmap["cursor"]["refactor"])
        self.assertEqual(r["ranked"][1]["A"], self.capmap["grok-build"]["refactor"])

    def test_equal_u_code_edit_tie_picks_grok_build(self):
        caps = self._temp_caps()
        r = D.route(CODE, self.capmap, caps)
        self.assertEqual(r["task_type"], "code_edit")
        self.assertFalse(r["lan"])
        self.assertEqual(len({row["U"] for row in r["ranked"]}), 1)
        self.assertEqual(r["ranked"][0]["harness"], "grok-build")
        self.assertEqual(r["ranked"][1]["harness"], "cursor")
        self.assertEqual(r["ranked"][0]["score"], r["ranked"][1]["score"])
        self.assertIn("fallback", r["decided_by"])

    def test_kill_still_exits_3(self):
        kill = os.path.join(self.tmp, "KILL-SWITCH")
        with open(kill, "w", encoding="utf-8") as f:
            f.write("stop\n")
        pkt = os.path.join(self.tmp, "PACKET-KILL.md")
        with open(pkt, "w", encoding="utf-8") as f:
            f.write(CODE)
        env = dict(os.environ)
        env["DISPATCH_KILL"] = kill
        env["DISPATCH_STATE"] = self.tmp
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p = self._subprocess_run(
            [sys.executable, os.path.join(ROOT, "dispatcher.py"), "route-choose", pkt],
            capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 3)
        self.assertIn("KILLED", p.stderr)
        self.assertIn(kill, p.stderr)
        self.assertEqual(p.stdout.strip(), "")
        self.assertNotIn("score", p.stdout)
        D.KILL = kill
        try:
            with self.assertRaises(SystemExit) as caught:
                D.check_kill()
            self.assertEqual(caught.exception.code, 3)
        finally:
            D.KILL = os.path.join(self.tmp, "KILL")

    def test_caps_json_unchanged_phase_6(self):
        path = os.path.join(ROOT, "caps.json")
        with open(path, "rb") as f:
            raw = f.read()
        self.assertEqual(raw, self._caps_raw)
        self.assertTrue(hashlib.sha256(raw).hexdigest().startswith(CAPS_SHA_PREFIX))
        caps = json.loads(raw.decode())
        for h in THREE:
            self.assertIs(caps[h]["enabled"], True)
            self.assertNotIn("daily_cap", caps[h])
            self.assertNotIn("test_call_cap", caps[h])
        self.assertIsNone(HC.CAPS["grok-build"])
        self.assertIsNone(HC.CAPS["cursor"])
        self.assertIn("No limits", caps["cursor"]["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
