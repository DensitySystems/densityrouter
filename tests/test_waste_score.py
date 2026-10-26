"""Phase 3: waste score over the whole window. stdlib unittest, temp dirs only.
Run: python3 tests/test_waste_score.py
"""
import copy, hashlib, json, os, shutil, sys, tempfile, unittest
from datetime import datetime, timedelta
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D
import usage_writer as UW

CAPS_SHA_PREFIX = "02a81b2eb0a8"


def _wall(y, mo, d, hh=12, mm=0, ss=0):
    return datetime(y, mo, d, hh, mm, ss).astimezone().replace(microsecond=0)


class WasteScoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._decl = copy.deepcopy(UW.DECLARATIONS)

    def tearDown(self):
        UW.DECLARATIONS.clear()
        UW.DECLARATIONS.update(self._decl)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _udir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        return d

    def _put(self, udir, h, snap):
        with open(os.path.join(udir, h + ".json"), "w") as f:
            json.dump(snap, f)
        return snap

    def _base(self, h, now, rem_pct, window_h, hours_to_reset, burn=0, read_at=None):
        """Fixture snapshot. Numbers here are test inputs, not the formula."""
        return {
            "harness": h,
            "remaining_pct": rem_pct,
            "window_len_h": window_h,
            "reset_at": (now + timedelta(hours=hours_to_reset)).isoformat(),
            "burn_pct_per_h": burn,
            "read_at": (now if read_at is None else read_at).isoformat(),
        }

    def _u(self, udir, h, now, snap):
        self._put(udir, h, snap)
        return D.usage_for(h, udir, now=now.timestamp())

    def test_missing_burn_is_not_zero(self):
        now = _wall(2026, 10, 25, 9)
        udir = self._udir("miss")
        fields = self._base("grok-build", now, 80, 168, 72, burn=0)
        present = dict(fields)
        missing = dict(fields)
        del missing["burn_pct_per_h"]
        u_missing, st_m = self._u(udir, "grok-build", now, missing)
        u_zero, st_z = self._u(self._udir("zero"), "grok-build", now, present)
        self.assertEqual(st_m, "fresh")
        self.assertEqual(st_z, "fresh")
        # Neutral: no waste term. Substituting 0 would boost, same as burn 0.
        self.assertAlmostEqual(u_missing, 0.80, places=6)
        self.assertGreater(u_zero, u_missing)

    def test_burn_unknown_uses_observed_rate(self):
        now = _wall(2026, 8, 1, 15)
        h = "agy-gemini"
        # Window started 10h ago. allowance 100, count 20 -> 2.0 pct/h.
        # remaining_pct is intentionally NOT 80, so a remaining-based guess
        # (5.0 pct/h) is a different rate than the counter.
        reset = now + timedelta(hours=24)
        decl = {x: {"allowance": 100, "window_len_h": 34, "reset_at": reset.isoformat()} for x in UW.HARNESSES}
        UW.DECLARATIONS[h] = dict(decl[h])
        udir = self._udir("obs")
        UW.record_call(h, n=20, now=now, decl=decl, udir=udir)
        snap = self._base(h, now, 50, 168, 24)
        del snap["burn_pct_per_h"]
        self._put(udir, h, snap)
        u, st = D.usage_for(h, udir, now=now.timestamp())
        explicit = dict(snap)
        explicit["burn_pct_per_h"] = 2.0
        u_exp, st_e = self._u(self._udir("obs-exp"), h, now, explicit)
        wrong = dict(explicit)
        wrong["burn_pct_per_h"] = 5.0
        u_wrong, _ = self._u(self._udir("obs-wrong"), h, now, wrong)
        self.assertEqual(st, "fresh")
        self.assertEqual(st_e, "fresh")
        self.assertAlmostEqual(u, u_exp, places=6)
        self.assertNotAlmostEqual(u, u_wrong, places=4)

    def test_burn_unknown_without_rate_is_neutral(self):
        now = _wall(2026, 4, 2, 8)
        udir = self._udir("neutral")
        snap = self._base("async-fake", now, 60, 24, 20, burn=0)
        del snap["burn_pct_per_h"]
        self.assertFalse(os.path.exists(os.path.join(udir, "async-fake.counter.json")))
        u, st = self._u(udir, "async-fake", now, snap)
        self.assertEqual(st, "fresh")
        self.assertAlmostEqual(u, 0.60, places=6)
        boosted, _ = self._u(self._udir("neutral-zero"), "async-fake", now, self._base("async-fake", now, 60, 24, 20, burn=0))
        self.assertGreater(boosted, u)

    def test_waste_visible_past_48h(self):
        now = _wall(2026, 7, 4, 10)
        # 72h to reset is past the old 48h gate. Burn 0 leaves the remainder unused.
        snap = self._base("grok-build", now, 70, 168, 72, burn=0)
        u, st = self._u(self._udir("past48"), "grok-build", now, snap)
        self.assertEqual(st, "fresh")
        self.assertGreater(u, 0.70)

    def test_no_clamp_ties_distinct_high_remainders(self):
        now = _wall(2026, 3, 3, 3)
        # Old formula: tl < 48 and unused > 0 -> min(1, rem*1.5) ties these at 1.
        hi = self._base("grok-build", now, 95, 24, 10, burn=0)
        lo = self._base("grok-build", now, 80, 24, 10, burn=0)
        u_hi, st_hi = self._u(self._udir("hi"), "grok-build", now, hi)
        u_lo, st_lo = self._u(self._udir("lo"), "grok-build", now, lo)
        self.assertEqual(st_hi, "fresh")
        self.assertEqual(st_lo, "fresh")
        self.assertGreater(u_lo, 1.0)
        self.assertGreater(u_hi, u_lo)

    def test_window_len_h_is_read(self):
        now = _wall(2026, 11, 11, 11)
        # Same remainder, burn 0, same hours_to_reset (20h). expected_unused = 0.80.
        # prox = clamp(1 - 20/w, 0, 1): 20h left of 24 is far from reset (prox 1/6);
        # 20h left of 168 is near reset (prox 148/168). The longer window scores higher.
        a = self._base("cursor", now, 80, 24, 20, burn=0)
        b = self._base("cursor", now, 80, 168, 20, burn=0)
        u24, _ = self._u(self._udir("w24"), "cursor", now, a)
        u168, _ = self._u(self._udir("w168"), "cursor", now, b)
        self.assertGreater(u168, u24)
        self.assertGreater(u24, 0.80)
        self.assertNotAlmostEqual(u24, u168, places=5)

    def test_stale_factor_half(self):
        now = _wall(2026, 1, 15, 18)
        fresh = self._base("grok-build", now, 80, 168, 48, burn=0)
        u_fresh, st_f = self._u(self._udir("sf"), "grok-build", now, fresh)

        def aged(hours, name):
            snap = self._base("grok-build", now, 80, 168, 48, burn=0,
                              read_at=now - timedelta(hours=hours))
            return self._u(self._udir(name), "grok-build", now, snap)

        u6, st6 = aged(6, "a6")
        u6p, st6p = self._u(
            self._udir("a6p"), "grok-build", now,
            self._base("grok-build", now, 80, 168, 48, burn=0,
                       read_at=now - timedelta(hours=6, seconds=1)))
        u24, st24 = aged(24, "a24")
        u24p, st24p = self._u(
            self._udir("a24p"), "grok-build", now,
            self._base("grok-build", now, 80, 168, 48, burn=0,
                       read_at=now - timedelta(hours=24, seconds=1)))
        self.assertEqual(st_f, "fresh")
        self.assertEqual(st6, "fresh")
        self.assertAlmostEqual(u6, u_fresh, places=6)
        self.assertEqual(st6p, "stale")
        self.assertAlmostEqual(u6p, u_fresh * 0.5, places=6)
        self.assertEqual(st24, "stale")
        self.assertAlmostEqual(u24, u_fresh * 0.5, places=6)
        self.assertEqual(st24p, "unknown(>24h)")
        self.assertEqual(u24p, D.U_DEFAULT)

    def test_config_gap_ranks_below_fresh_positive(self):
        now = _wall(2026, 10, 25, 12)
        h = "grok-build"
        udir = self._udir("gap")
        gap = UW.write_snapshot(h, now=now, udir=udir)  # shipped declarations
        self.assertEqual(gap["state"], "config-gap")
        self.assertFalse(os.path.exists(os.path.join(udir, h + ".json")))
        self.assertTrue(UW._missing(UW._fields_for(h, None)))
        u, st = D.usage_for(h, udir, now=now.timestamp())
        self.assertEqual(st, "config-gap")
        self.assertLessEqual(u, D.U_DEFAULT)
        # 1% remaining that will be burned (no waste boost) is the small fresh U.
        small = self._base("agy-gemini", now, 1, 24, 10, burn=5)
        u_small, st_small = self._u(self._udir("gap-small"), "agy-gemini", now, small)
        self.assertEqual(st_small, "fresh")
        self.assertAlmostEqual(u_small, 0.01, places=6)
        self.assertLess(u, u_small)
        wide = self._base("async-fake", now, 80, 168, 96, burn=0)
        u_wide, st_wide = self._u(self._udir("gap-wide"), "async-fake", now, wide)
        self.assertEqual(st_wide, "fresh")
        self.assertLess(u, u_wide)
        # File absence alone is not config-gap once the declaration is complete.
        saved = copy.deepcopy(UW.DECLARATIONS)
        try:
            reset = (now + timedelta(hours=12)).isoformat()
            for name in UW.HARNESSES:
                UW.DECLARATIONS[name] = {"allowance": 10, "window_len_h": 24, "reset_at": reset}
            bare = self._udir("bare")
            u_bare, st_bare = D.usage_for(h, bare, now=now.timestamp())
            self.assertEqual(st_bare, "unknown")
            self.assertEqual(u_bare, D.U_DEFAULT)
        finally:
            UW.DECLARATIONS.clear()
            UW.DECLARATIONS.update(saved)

    def test_caps_json_unchanged_phase_3(self):
        h = hashlib.sha256()
        with open(os.path.join(ROOT, "caps.json"), "rb") as f:
            h.update(f.read())
        self.assertTrue(h.hexdigest().startswith(CAPS_SHA_PREFIX))

    def test_s2_no_boost_when_expected_unused_nonpositive(self):
        now = _wall(2026, 5, 5, 5)
        # Burn well above the rate that would exhaust either remainder.
        high = self._base("grok-build", now, 80, 24, 12, burn=20)
        low = self._base("agy-gemini", now, 30, 24, 12, burn=20)
        u_high, st_h = self._u(self._udir("s2h"), "grok-build", now, high)
        u_low, st_l = self._u(self._udir("s2l"), "agy-gemini", now, low)
        self.assertEqual(st_h, "fresh")
        self.assertEqual(st_l, "fresh")
        self.assertAlmostEqual(u_high, 0.80, places=6)
        self.assertAlmostEqual(u_low, 0.30, places=6)
        self.assertGreater(u_high, u_low)

    def test_s4_missing_burn_no_rate_neutral(self):
        now = _wall(2026, 2, 2, 14)
        snap = self._base("async-fake", now, 45, 48, 30)
        del snap["burn_pct_per_h"]
        udir = self._udir("s4")
        u, st = self._u(udir, "async-fake", now, snap)
        self.assertEqual(st, "fresh")
        self.assertAlmostEqual(u, 0.45, places=6)
        self.assertFalse(os.path.exists(os.path.join(udir, "async-fake.counter.json")))

    def test_s5_missing_burn_uses_observed_rate(self):
        now = _wall(2026, 9, 9, 9)
        h = "grok-build"
        # 5h elapsed, allowance 50, count 10 -> burn 4.0 pct/h.
        # remaining 40 would imply 12 pct/h if the counter count were ignored.
        reset = now + timedelta(hours=20)
        decl = {x: {"allowance": 50, "window_len_h": 25, "reset_at": reset.isoformat()} for x in UW.HARNESSES}
        UW.DECLARATIONS[h] = dict(decl[h])
        udir = self._udir("s5")
        UW.record_call(h, n=10, now=now, decl=decl, udir=udir)
        snap = self._base(h, now, 40, 168, 20)
        del snap["burn_pct_per_h"]
        u, st = self._u(udir, h, now, snap)
        match = dict(snap)
        match["burn_pct_per_h"] = 4.0
        u_match, _ = self._u(self._udir("s5m"), h, now, match)
        as_zero = dict(snap)
        as_zero["burn_pct_per_h"] = 0
        u_zero, _ = self._u(self._udir("s5z"), h, now, as_zero)
        self.assertEqual(st, "fresh")
        self.assertAlmostEqual(u, u_match, places=6)
        self.assertGreater(u_zero, u)

    def test_s6_stale_half(self):
        now = _wall(2026, 6, 6, 16)
        fresh = self._base("agy-gemini", now, 80, 168, 80, burn=0.1)
        u_fresh, st_f = self._u(self._udir("s6f"), "agy-gemini", now, fresh)
        stale = self._base("agy-gemini", now, 80, 168, 80, burn=0.1,
                           read_at=now - timedelta(hours=12))
        u_stale, st_s = self._u(self._udir("s6s"), "agy-gemini", now, stale)
        self.assertEqual(st_f, "fresh")
        self.assertEqual(st_s, "stale")
        self.assertAlmostEqual(u_stale, u_fresh * 0.5, places=6)
        self.assertGreater(u_fresh, 0.80)  # waste applied, then halved

    def test_s8_aged_external_below_fresh(self):
        now = _wall(2026, 12, 1, 8)
        # External reading (burn present, not a counter snapshot), older than 24h.
        aged = self._base("grok-build", now, 90, 168, 100, burn=0.05,
                          read_at=now - timedelta(hours=30))
        fresh = self._base("grok-build", now, 90, 168, 100, burn=0.05)
        u_aged, st_a = self._u(self._udir("s8a"), "grok-build", now, aged)
        u_fresh, st_f = self._u(self._udir("s8f"), "grok-build", now, fresh)
        self.assertEqual(st_a, "unknown(>24h)")
        self.assertEqual(u_aged, 0.3)
        self.assertEqual(st_f, "fresh")
        self.assertLess(u_aged, u_fresh)

    def test_s7_counter_allowance_above_fresh_5pct(self):
        now = _wall(2026, 10, 25, 12)
        h = "grok-build"
        reset = now + timedelta(hours=96)  # 4 days
        decl = {x: {"allowance": 100, "window_len_h": 168, "reset_at": reset.isoformat()}
                for x in UW.HARNESSES}
        udir = self._udir("s7")
        UW.record_call(h, n=20, now=now, decl=decl, udir=udir)
        snap = UW.write_snapshot(h, now=now, decl=decl, udir=udir)
        self.assertEqual(snap["state"], "counter")
        self.assertEqual(snap["source"], "own-counter")
        self.assertEqual(snap["remaining_pct"], 80.0)
        self.assertEqual(snap["window_len_h"], 168)
        u_w, st_w = D.usage_for(h, udir, now=now.timestamp())
        self.assertEqual(st_w, "fresh")
        self.assertNotEqual(u_w, 0.3)
        self.assertGreater(u_w, 0.3)
        # Known burn, 5h window, burn 0: the strongest waste boost on 5% left.
        low = self._base("agy-gemini", now, 5, 5, 4, burn=0)
        u5, st5 = self._u(self._udir("s7low"), "agy-gemini", now, low)
        self.assertEqual(st5, "fresh")
        self.assertGreater(u_w / u5, 1.5 ** 1.5)

    def test_s3_u_ratio(self):
        now = _wall(2026, 10, 1, 0)
        tl = 6 * 24.0  # reset in 6 days
        rem = 80
        # Tiny positive expected_unused (0.001) must still clear the bar.
        eu = 0.001
        burn_frac = (0.80 - eu) / tl
        burn_pct = burn_frac * 100.0
        a = self._base("grok-build", now, rem, 168, tl, burn=burn_pct)
        b = self._base("agy-gemini", now, rem, 168, tl, burn=5.0)  # expected_unused < 0
        # Zero burn: a larger waste on the same window scores at least as high.
        a_big = self._base("async-fake", now, rem, 168, tl, burn=0)
        u_a, st_a = self._u(self._udir("s3a"), "grok-build", now, a)
        u_b, st_b = self._u(self._udir("s3b"), "agy-gemini", now, b)
        u_big, st_big = self._u(self._udir("s3big"), "async-fake", now, a_big)
        self.assertEqual(st_a, "fresh")
        self.assertEqual(st_b, "fresh")
        self.assertEqual(st_big, "fresh")
        self.assertAlmostEqual(u_b, 0.80, places=6)
        self.assertGreater(u_a / u_b, (0.9 / 0.8) ** 1.5)
        self.assertGreater(u_big / u_b, (0.9 / 0.8) ** 1.5)
        self.assertGreater(u_big, u_a)

    def test_route_scores_cursor_when_caps_flags_are_off(self):
        cap = D.load(os.path.join(ROOT, "capability-map.json"))
        on_disk = D.load(os.path.join(ROOT, "caps.json"))
        self.assertIs(on_disk["cursor"]["enabled"], True)
        self.assertNotIn("daily_cap", on_disk["cursor"])
        caps = json.loads(json.dumps(on_disk))
        caps["cursor"]["enabled"] = False
        caps["cursor"]["daily_cap"] = 0
        caps["cursor"]["used_today"] = 5
        now = _wall(2026, 10, 25, 12)
        r = D.route("fix the failing test", cap, caps, now=now.timestamp())
        self.assertIn("cursor", [row["harness"] for row in r["ranked"]])
        self.assertFalse(any(h == "cursor" for h, _why in r["skipped"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
