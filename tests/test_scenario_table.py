"""Phase 7 scenario table. These assertions are the oracle.
Stdlib unittest. Temp usage dirs and an in-memory caps copy only. Never writes caps.json.
DISPATCH_LIVE is never set on os.environ.
Run: python3 tests/test_scenario_table.py
"""
import copy, hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D
import live as LV
import usage_writer as UW

CAPS_SHA_PREFIX = "02a81b2eb0a8"
POOL = ["grok-build", "cursor", "agy-gemini"]
CODE_EDIT = "Objective: Fix the build script.\n"
REFACTOR = "Objective: Refactor the repo-wide helpers.\n"
RESEARCH = "Objective: Research the eval notes, read-only.\n"


def _wall(y, mo, d, hh=12, mm=0, ss=0):
    return datetime(y, mo, d, hh, mm, ss).astimezone().replace(microsecond=0)


class ScenarioTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL", "DISPATCH_LIVE")}
        os.environ["DISPATCH_STATE"] = self.tmp
        os.environ.pop("DISPATCH_LIVE", None)
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL")
        os.environ["DISPATCH_KILL"] = D.KILL
        self._subprocess_run = subprocess.run

        def boom(*a, **k):
            raise AssertionError("subprocess.run")

        subprocess.run = boom
        self._decl = copy.deepcopy(UW.DECLARATIONS)
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = copy.deepcopy(D.load(os.path.join(ROOT, "caps.json")))
        self.assertIs(self.caps["cursor"]["enabled"], True)
        self.assertNotIn("daily_cap", self.caps["cursor"])
        self.assertNotIn("test_call_cap", self.caps["cursor"])
        self.assertEqual(self.caps["cursor"]["used_today"], 0)
        self.now = _wall(2026, 10, 25, 12)
        self.now_ts = self.now.timestamp()
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        self.assertIsNone(D.HEALTH_COOLDOWN_S)

    def tearDown(self):
        subprocess.run = self._subprocess_run
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

    def _snap(self, h, rem_pct, window_h, hours_to_reset, burn=0, read_at=None, now=None):
        now = self.now if now is None else now
        snap = {
            "harness": h,
            "remaining_pct": rem_pct,
            "window_len_h": window_h,
            "reset_at": (now + timedelta(hours=hours_to_reset)).isoformat(),
            "read_at": (now if read_at is None else read_at).isoformat(),
        }
        if burn is not None:
            snap["burn_pct_per_h"] = burn
        return snap

    def _route(self, text, udir, now=None):
        return D.route(text, self.capmap, self.caps, udir=udir, now=self.now_ts if now is None else now)

    def _row(self, route, harness):
        return next(row for row in route["ranked"] if row["harness"] == harness)

    def test_scenario_s1_code_edit_config_gap_winner_grok_build(self):
        udir = self._udir("s1")
        r = self._route(CODE_EDIT, udir)
        self.assertEqual(r["task_type"], "code_edit")
        self.assertFalse(r["lan"])
        self.assertEqual([row["harness"] for row in r["ranked"]], POOL)
        self.assertEqual(r["decided_by"], "score tie -> fallback order")
        self.assertFalse(r["phase1"])
        for row in r["ranked"]:
            self.assertEqual(row["usage"], "config-gap")
            self.assertFalse(str(row["usage"]).startswith("unknown"))
            self.assertEqual(row["U"], 0.0)
            self.assertNotEqual(row["U"], 0.3)
            self.assertEqual(row["H"], 1.0)
        gb = self._row(r, "grok-build")
        cur = self._row(r, "cursor")
        self.assertEqual(gb["A"], 0.9)
        self.assertEqual(cur["A"], 0.9)
        self.assertEqual(r["ranked"][0]["harness"], "grok-build")
        self.assertEqual(r["basis"], "ability-only")

    def test_scenario_s2_known_burn_consumed_no_waste_boost(self):
        # Reset in 1 day, burn high enough that expected_unused <= 0 for every remainder.
        remain = {"grok-build": 80, "cursor": 55, "agy-gemini": 30}
        udir = self._udir("s2")
        for h, rem in remain.items():
            self._put(udir, h, self._snap(h, rem, 24, 24, burn=20))
        r = self._route(CODE_EDIT, udir)
        self.assertFalse(r["lan"])
        rows = [self._row(r, h) for h in POOL]
        for row in rows:
            self.assertEqual(row["usage"], "fresh")
            self.assertEqual(row["H"], 1.0)
            self.assertAlmostEqual(row["U"], remain[row["harness"]] / 100.0, places=3)
        by_rem = sorted(rows, key=lambda row: remain[row["harness"]], reverse=True)
        us = [row["U"] for row in by_rem]
        self.assertEqual(us, sorted(us, reverse=True))
        for hi, lo in zip(by_rem, by_rem[1:]):
            self.assertGreater(remain[hi["harness"]], remain[lo["harness"]])
            self.assertGreater(hi["U"], lo["U"])
        # Higher remaining is not placed below a lower one.
        self.assertEqual([row["harness"] for row in r["ranked"]], POOL)

    def test_scenario_s3_weekly_waste_beats_cursor_on_refactor(self):
        udir = self._udir("s3")
        # 168h window, reset in 6 days. grok-build burn 0 leaves expected_unused > 0.
        # cursor burn 5 pct/h over 144h leaves expected_unused <= 0 (U stays at 0.80).
        self._put(udir, "grok-build", self._snap("grok-build", 80, 168, 6 * 24, burn=0))
        self._put(udir, "cursor", self._snap("cursor", 80, 168, 6 * 24, burn=5.0))
        r = self._route(REFACTOR, udir)
        self.assertEqual(r["task_type"], "refactor")
        self.assertFalse(r["lan"])
        gb = self._row(r, "grok-build")
        cur = self._row(r, "cursor")
        self.assertEqual(gb["usage"], "fresh")
        self.assertEqual(cur["usage"], "fresh")
        self.assertEqual(gb["H"], cur["H"])
        self.assertAlmostEqual(cur["U"], 0.80, places=3)
        self.assertGreater(gb["U"], cur["U"])
        self.assertEqual(r["ranked"][0]["harness"], "grok-build")
        self.assertLess(
            [row["harness"] for row in r["ranked"]].index("grok-build"),
            [row["harness"] for row in r["ranked"]].index("cursor"))

    def test_scenario_s4_missing_burn_no_rate_is_neutral(self):
        udir = self._udir("s4")
        for h in POOL:
            snap = self._snap(h, 60, 24, 24, burn=None)
            self.assertNotIn("burn_pct_per_h", snap)
            self._put(udir, h, snap)
            self.assertFalse(os.path.exists(os.path.join(udir, h + ".counter.json")))
        r = self._route(CODE_EDIT, udir)
        for row in r["ranked"]:
            self.assertEqual(row["usage"], "fresh")
            self.assertAlmostEqual(row["U"], 0.60, places=3)
        boosted_dir = self._udir("s4-burn0")
        self._put(boosted_dir, "grok-build", self._snap("grok-build", 60, 24, 24, burn=0))
        boosted = self._route(CODE_EDIT, boosted_dir)
        self.assertGreater(self._row(boosted, "grok-build")["U"], self._row(r, "grok-build")["U"])

    def test_scenario_s5_missing_burn_uses_observed_rate(self):
        # Counter: allowance 100, count 10, window started 10h ago -> 1.0 pct/h.
        # Snapshot burn is absent. Reset on the snapshot is 1 day. expected_unused > 0, so U > remaining.
        h = "grok-build"
        UW.DECLARATIONS[h] = {
            "allowance": 100,
            "window_len_h": 24,
            "reset_at": (self.now + timedelta(hours=14)).isoformat(),
        }
        udir = self._udir("s5")
        UW.record_call(h, n=10, now=self.now, udir=udir)
        snap = self._snap(h, 80, 24, 24, burn=None)
        self.assertNotIn("burn_pct_per_h", snap)
        self._put(udir, h, snap)
        r = self._route(CODE_EDIT, udir)
        row = self._row(r, h)
        self.assertEqual(row["usage"], "fresh")
        self.assertGreater(row["U"], 0.80)
        explicit = self._udir("s5-explicit")
        self._put(explicit, h, self._snap(h, 80, 24, 24, burn=1.0))
        wrong = self._udir("s5-wrong")
        self._put(wrong, h, self._snap(h, 80, 24, 24, burn=5.0))
        self.assertEqual(row["U"], self._row(self._route(CODE_EDIT, explicit), h)["U"])
        self.assertGreater(row["U"], self._row(self._route(CODE_EDIT, wrong), h)["U"])

    def test_scenario_s6_stale_half(self):
        # Known fresh waste (burn 0, remainder left unused). Age 12h is inside (6h, 24h].
        fresh_dir = self._udir("s6-fresh")
        stale_dir = self._udir("s6-stale")
        self._put(fresh_dir, "grok-build", self._snap("grok-build", 80, 168, 48, burn=0))
        self._put(stale_dir, "grok-build", self._snap(
            "grok-build", 80, 168, 48, burn=0, read_at=self.now - timedelta(hours=12)))
        fresh = self._row(self._route(CODE_EDIT, fresh_dir), "grok-build")
        stale = self._row(self._route(CODE_EDIT, stale_dir), "grok-build")
        self.assertEqual(fresh["usage"], "fresh")
        self.assertEqual(stale["usage"], "stale")
        self.assertGreater(fresh["U"], 0.80)
        self.assertAlmostEqual(stale["U"], fresh["U"] * 0.5, delta=0.001)

    def test_scenario_s7_counter_allowance_beats_fresh_5pct_on_research(self):
        # Weekly counter, 20 of allowance 100 used, reset in 4 days, no hand-written external readout.
        # agy-gemini: fresh 5% with known burn 0 on a 5h window (the binding waste case in usage_for).
        h = "grok-build"
        udir = self._udir("s7")
        reset = self.now + timedelta(hours=96)
        UW.DECLARATIONS[h] = {"allowance": 100, "window_len_h": 168, "reset_at": reset.isoformat()}
        UW.record_call(h, n=20, now=self.now, udir=udir)
        snap = UW.write_snapshot(h, now=self.now, udir=udir)
        self.assertEqual(snap["state"], "counter")
        self.assertEqual(snap["source"], "own-counter")
        self.assertEqual(snap["remaining_pct"], 80.0)
        self.assertEqual(snap["window_len_h"], 168)
        self.assertTrue(os.path.exists(os.path.join(udir, h + ".counter.json")))
        self._put(udir, "agy-gemini", self._snap("agy-gemini", 5, 5, 4, burn=0))
        r = self._route(RESEARCH, udir)
        self.assertEqual(r["task_type"], "research")
        self.assertFalse(r["lan"])
        gb = self._row(r, h)
        agy = self._row(r, "agy-gemini")
        self.assertEqual(gb["usage"], "fresh")
        self.assertEqual(agy["usage"], "fresh")
        self.assertEqual(gb["H"], agy["H"])
        self.assertGreater(gb["H"], 0)
        self.assertGreater(gb["U"], 0.80)
        names = [row["harness"] for row in r["ranked"]]
        self.assertLess(names.index("grok-build"), names.index("agy-gemini"))

    def test_scenario_s8_aged_external_ranks_below_fresh(self):
        udir = self._udir("s8")
        # External readout (burn present), older than 24h, no counter of our own.
        self._put(udir, "cursor", self._snap(
            "cursor", 90, 168, 100, burn=0.05, read_at=self.now - timedelta(hours=30)))
        self.assertFalse(os.path.exists(os.path.join(udir, "cursor.counter.json")))
        # Fresh positive usable remainder (burn 0, expected_unused > 0).
        self._put(udir, "grok-build", self._snap("grok-build", 80, 168, 48, burn=0))
        r = self._route(CODE_EDIT, udir)
        aged = self._row(r, "cursor")
        fresh = self._row(r, "grok-build")
        self.assertEqual(aged["usage"], "unknown(>24h)")
        self.assertEqual(aged["U"], D.U_DEFAULT)
        self.assertEqual(fresh["usage"], "fresh")
        self.assertGreater(fresh["U"], 0)
        names = [row["harness"] for row in r["ranked"]]
        self.assertLess(names.index("grok-build"), names.index("cursor"))

    def test_explicit_lan_still_grok_build_only_in_scenarios(self):
        text = "Placement: LAN\nObjective: Open a browser on the website.\n"
        r = self._route(text, None)
        self.assertTrue(r["lan"])
        self.assertEqual(r["task_type"], "browser")
        self.assertEqual([row["harness"] for row in r["ranked"]], ["grok-build"])
        self.assertEqual(sorted(h for h, _why in r["skipped"]), ["agy-gemini", "cursor"])
        self.assertNotIn("async-fake", [row["harness"] for row in r["ranked"]])
        self.assertNotIn("async-fake", [h for h, _why in r["skipped"]])

    def test_live_switch_still_off(self):
        state = os.path.join(self.tmp, "state")
        os.makedirs(state)
        self.assertFalse(os.path.exists(os.path.join(state, "LIVE")))
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        env = {"DISPATCH_STATE": state}
        self.assertNotEqual(env.get("DISPATCH_LIVE"), "1")
        self.assertFalse(LV.live_enabled(env))
        self.assertFalse(LV.live_enabled({"DISPATCH_STATE": state, "DISPATCH_LIVE": "0"}))
        self.assertFalse(LV.live_enabled({"DISPATCH_STATE": state, "DISPATCH_LIVE": ""}))

    def test_caps_json_unchanged_phase_7(self):
        path = os.path.join(ROOT, "caps.json")
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            digest.update(f.read())
        self.assertTrue(digest.hexdigest().startswith(CAPS_SHA_PREFIX))
        on_disk = D.load(path)
        self.assertIs(on_disk["cursor"]["enabled"], True)
        self.assertNotIn("daily_cap", on_disk["cursor"])
        self.assertNotIn("test_call_cap", on_disk["cursor"])
        self.assertEqual(on_disk["cursor"]["used_today"], 0)
        self.assertIsNone(D.HEALTH_COOLDOWN_S)

    def test_pool_exactly_three_in_scenarios(self):
        r = self._route(CODE_EDIT, self._udir("pool"))
        names = [row["harness"] for row in r["ranked"]] + [h for h, _why in r["skipped"]]
        self.assertEqual(sorted(names), sorted(POOL))
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)
        self.assertNotIn("async-fake", names)
        self.assertNotIn("local", names)
        self.assertEqual(list(D.FALLBACK), POOL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
