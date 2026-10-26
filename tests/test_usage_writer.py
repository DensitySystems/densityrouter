"""Phase 2: one usage writer per harness. stdlib unittest, temp dirs only.
Run: python3 tests/test_usage_writer.py
"""
import hashlib, json, os, re, shutil, sys, tempfile, unittest
from datetime import datetime, timedelta
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D
import usage_writer as UW

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
SNAP_KEYS = ("harness", "remaining_pct", "window_len_h", "reset_at", "burn_pct_per_h", "read_at", "state", "source")


def _wall(y, mo, d, hh=12, mm=0, ss=0):
    return datetime(y, mo, d, hh, mm, ss).astimezone().replace(microsecond=0)


class UsageWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved_env = os.environ.get("DISPATCH_USAGE_DIR")
        os.environ["DISPATCH_USAGE_DIR"] = self.tmp  # forgotten udir stays off the default tree
        self._caps = os.path.join(ROOT, "caps.json")
        self._calls = os.path.join(ROOT, "state", "calls.json")
        self._finger = (self._sha(self._caps), self._sha(self._calls), os.path.isfile(self._calls))

    def tearDown(self):
        try:
            self.assertEqual(
                (self._sha(self._caps), self._sha(self._calls), os.path.isfile(self._calls)),
                self._finger)
        finally:
            if self._saved_env is None:
                os.environ.pop("DISPATCH_USAGE_DIR", None)
            else:
                os.environ["DISPATCH_USAGE_DIR"] = self._saved_env
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _sha(self, path):
        if not os.path.isfile(path):
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read())
        return h.hexdigest()

    def _decl(self, h, allowance, window_h, reset):
        if isinstance(reset, datetime):
            reset = reset.isoformat()
        return {h: {"allowance": allowance, "window_len_h": window_h, "reset_at": reset}}

    def _udir(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        return path

    def _assert_snapshot_shape(self, snap, h, now):
        self.assertEqual(tuple(snap.keys()), SNAP_KEYS)
        self.assertEqual(snap["harness"], h)
        self.assertEqual(snap["state"], "counter")
        self.assertEqual(snap["source"], "own-counter")
        self.assertIsInstance(snap["remaining_pct"], (int, float))
        self.assertGreaterEqual(snap["remaining_pct"], 0)
        self.assertLessEqual(snap["remaining_pct"], 100)
        self.assertIsInstance(snap["window_len_h"], (int, float))
        self.assertIsInstance(snap["burn_pct_per_h"], (int, float))
        self.assertRegex(snap["reset_at"], ISO_RE)
        self.assertRegex(snap["read_at"], ISO_RE)
        self.assertEqual(datetime.fromisoformat(snap["read_at"]), now)
        self.assertNotIn("unknown", snap["state"])

    def _assert_disk(self, udir, h, snap):
        path = os.path.join(udir, h + ".json")
        with open(path) as f:
            disk = json.load(f)
        self.assertEqual(disk["harness"], snap["harness"])
        self.assertEqual(disk["remaining_pct"], snap["remaining_pct"])
        self.assertEqual(disk["window_len_h"], snap["window_len_h"])
        self.assertEqual(disk["reset_at"], snap["reset_at"])
        self.assertEqual(disk["read_at"], snap["read_at"])
        self.assertEqual(disk["state"], "counter")
        self.assertEqual(disk["source"], "own-counter")
        self.assertAlmostEqual(disk["burn_pct_per_h"], snap["burn_pct_per_h"])
        names = os.listdir(udir)
        self.assertNotIn(h + ".json.", "".join(names))
        temps = [n for n in names if n.startswith(h) and n not in (h + ".json", h + ".counter.json")]
        self.assertEqual(temps, [])

    def test_writer_grok_build_snapshot_fields(self):
        h = "grok-build"
        udir = self._udir("grok-build")
        now = _wall(2026, 10, 25, 15)
        reset = now + timedelta(hours=12)
        decl = self._decl(h, 3, 24, reset)
        counter = UW.record_call(h, n=1, now=now, decl=decl, udir=udir)
        self.assertEqual(counter["count"], 1)
        self.assertEqual(set(counter), {"harness", "window_start", "reset_at", "count"})
        self.assertEqual(datetime.fromisoformat(counter["window_start"]), reset - timedelta(hours=24))
        snap = UW.write_snapshot(h, now=now, decl=decl, udir=udir)
        self._assert_snapshot_shape(snap, h, now)
        self.assertEqual(snap["window_len_h"], 24)
        self.assertEqual(snap["remaining_pct"], round(max(0, 3 - 1) / 3 * 100.0, 1))
        self.assertEqual(snap["remaining_pct"], 66.7)
        self.assertEqual(datetime.fromisoformat(snap["reset_at"]), reset)
        used_pct = 1 / 3 * 100.0
        self.assertAlmostEqual(snap["burn_pct_per_h"], used_pct / 12.0, places=6)
        self._assert_disk(udir, h, snap)

    def test_writer_agy_gemini_snapshot_fields(self):
        h = "agy-gemini"
        udir = self._udir("agy")
        now = _wall(2026, 7, 1, 8)
        # 3 minutes into a 24h window: raw elapsed is under the 0.1h floor.
        reset = now + timedelta(hours=24) - timedelta(minutes=3)
        decl = self._decl(h, 10, 24, reset)
        UW.record_call(h, n=2, now=now, decl=decl, udir=udir)
        snap = UW.write_snapshot(h, now=now, decl=decl, udir=udir)
        self._assert_snapshot_shape(snap, h, now)
        self.assertEqual(snap["remaining_pct"], 80.0)
        self.assertEqual(snap["window_len_h"], 24)
        self.assertEqual(snap["burn_pct_per_h"], 200.0)
        self.assertEqual(datetime.fromisoformat(snap["reset_at"]), reset)
        self._assert_disk(udir, h, snap)

    def test_writer_refuses_async_fake(self):  # async-fake is not part of the router
        udir = self._udir("bot")
        with self.assertRaises(ValueError):
            UW.write_snapshot("async-fake", now=_wall(2026, 8, 2, 9), udir=udir)
        self.assertEqual(os.listdir(udir), [])

    def test_writer_cursor_snapshot_fields(self):
        h = "cursor"
        now = _wall(2026, 9, 3, 10)
        reset = now + timedelta(hours=10)
        decl = self._decl(h, 50, 48, reset)
        env_dir = self._udir("env")
        arg_dir = self._udir("arg")
        os.environ["DISPATCH_USAGE_DIR"] = env_dir
        UW.record_call(h, now=now, decl=decl)          # n defaults to 1; dir from the env
        UW.record_call(h, n=4, now=now, decl=decl)
        snap = UW.write_snapshot(h, now=now, decl=decl)
        self._assert_snapshot_shape(snap, h, now)
        self.assertEqual(snap["remaining_pct"], 90.0)
        self.assertEqual(snap["window_len_h"], 48)
        used_pct = 5 / 50 * 100.0
        self.assertAlmostEqual(snap["burn_pct_per_h"], used_pct / 38.0, places=6)
        self.assertTrue(os.path.isfile(os.path.join(env_dir, h + ".json")))
        self.assertTrue(os.path.isfile(os.path.join(env_dir, h + ".counter.json")))
        with open(os.path.join(env_dir, h + ".counter.json")) as f:
            self.assertEqual(json.load(f)["count"], 5)
        # Function arg overrides the env dir and does not read that counter.
        snap_arg = UW.write_snapshot(h, now=now, decl=decl, udir=arg_dir)
        self.assertEqual(snap_arg["remaining_pct"], 100.0)
        self.assertEqual(snap_arg["source"], "own-counter")
        self.assertTrue(os.path.isfile(os.path.join(arg_dir, h + ".json")))
        self.assertFalse(os.path.exists(os.path.join(arg_dir, h + ".counter.json")))
        self._assert_disk(env_dir, h, snap)
        os.environ.pop("DISPATCH_USAGE_DIR", None)
        self.assertEqual(UW.resolve_usage_dir(), UW.C.path("usage_dir"))
        self.assertEqual(UW.resolve_usage_dir(arg_dir), arg_dir)

    def test_unset_allowance_is_config_gap(self):
        for h in UW.HARNESSES:
            self.assertEqual(
                UW.DECLARATIONS[h],
                {"allowance": None, "window_len_h": None, "reset_at": None})
        ghost = os.path.join(self.tmp, "ghost")
        os.environ["DISPATCH_USAGE_DIR"] = ghost
        local_off = datetime.now().astimezone().strftime("%z")
        local_off = local_off[:3] + ":" + local_off[3:]
        for h in UW.HARNESSES:
            snap = UW.write_snapshot(h)  # shipped DECLARATIONS, no decl override
            self.assertEqual(snap["harness"], h)
            self.assertEqual(snap["state"], "config-gap")
            self.assertEqual(snap["missing"], ["allowance", "window_len_h", "reset_at"])
            self.assertEqual(set(snap), {"harness", "state", "missing", "read_at"})
            self.assertRegex(snap["read_at"], ISO_RE)
            self.assertTrue(snap["read_at"].endswith(local_off))
            blob = json.dumps(snap)
            self.assertNotIn("0.3", blob)
            self.assertNotIn("unknown", blob)
            self.assertNotIn("remaining_pct", snap)
            u, st = D.usage_for(h, ghost)
            self.assertEqual(st, "unknown")
            self.assertEqual(u, D.U_DEFAULT)
        self.assertFalse(os.path.isdir(ghost))
        # One field unset is still a gap, and still writes no snapshot.
        now = _wall(2026, 10, 25, 11)
        reset = (now + timedelta(hours=12)).isoformat()
        partial = self._udir("partial")
        cases = (
            ({"allowance": None, "window_len_h": 24, "reset_at": reset}, ["allowance"]),
            ({"allowance": 10, "window_len_h": None, "reset_at": reset}, ["window_len_h"]),
            ({"allowance": 10, "window_len_h": 24, "reset_at": None}, ["reset_at"]),
        )
        for i, (fields, missing) in enumerate(cases):
            sub = os.path.join(partial, str(i))
            snap = UW.write_snapshot("grok-build", now=now, decl=fields, udir=sub)
            self.assertEqual(snap["state"], "config-gap")
            self.assertEqual(snap["missing"], missing)
            self.assertNotIn("0.3", json.dumps(snap))
            self.assertFalse(os.path.exists(os.path.join(sub, "grok-build.json")))
        blank = os.path.join(self.tmp, "blank.json")
        with open(blank, "w") as f:
            json.dump({h: {"allowance": None, "window_len_h": None, "reset_at": None} for h in UW.HARNESSES}, f)
        blank_out = os.path.join(self.tmp, "blank-out")
        snap = UW.write_snapshot("cursor", decl=blank, udir=blank_out)
        self.assertEqual(snap["state"], "config-gap")
        self.assertFalse(os.path.isdir(blank_out))

    def test_counter_plus_declaration_is_not_unknown(self):
        now = datetime.now().astimezone().replace(microsecond=0)
        reset = now + timedelta(hours=30)
        udir = self._udir("own")
        decl_path = os.path.join(self.tmp, "decl.json")
        decl = {}
        for h in UW.HARNESSES:
            decl[h] = {"allowance": 100, "window_len_h": 168, "reset_at": reset.isoformat()}
        with open(decl_path, "w") as f:
            json.dump(decl, f)
        for h in UW.HARNESSES:
            self.assertFalse(os.path.exists(os.path.join(udir, h + ".json")))
            UW.record_call(h, n=3, now=now, decl=decl_path, udir=udir)
            self.assertFalse(os.path.exists(os.path.join(udir, h + ".json")))
            self.assertTrue(os.path.isfile(os.path.join(udir, h + ".counter.json")))
            snap = UW.write_snapshot(h, now=now, decl=decl_path, udir=udir)
            self.assertEqual(snap["state"], "counter")
            self.assertEqual(snap["source"], "own-counter")
            self.assertEqual(snap["remaining_pct"], 97.0)
            u, st = D.usage_for(h, udir, now=now.timestamp())
            self.assertNotEqual(st, "unknown")
            self.assertFalse(str(st).startswith("unknown"))
            self.assertEqual(st, "fresh")

    def test_window_rollover_drops_old_count(self):
        h = "cursor"
        udir = self._udir("roll")
        T0 = _wall(2026, 6, 15, 12)
        window_h = 10
        allowance = 40
        decl = self._decl(h, allowance, window_h, T0)
        now1 = T0 - timedelta(hours=4)
        UW.record_call(h, n=5, now=now1, decl=decl, udir=udir)
        UW.record_call(h, n=3, now=now1, decl=decl, udir=udir)
        with open(os.path.join(udir, h + ".counter.json")) as f:
            self.assertEqual(json.load(f)["count"], 8)
        snap1 = UW.write_snapshot(h, now=now1, decl=decl, udir=udir)
        self.assertEqual(snap1["remaining_pct"], 80.0)
        self.assertAlmostEqual(snap1["burn_pct_per_h"], 20.0 / 6.0, places=6)
        # One second before reset still keeps the count.
        UW.record_call(h, n=1, now=T0 - timedelta(seconds=1), decl=decl, udir=udir)
        with open(os.path.join(udir, h + ".counter.json")) as f:
            held = json.load(f)
        self.assertEqual(held["count"], 9)
        self.assertEqual(datetime.fromisoformat(held["reset_at"]), T0)
        # The boundary starts a new window. The 9 does not carry.
        rolled = UW.record_call(h, n=2, now=T0, decl=decl, udir=udir)
        self.assertEqual(rolled["count"], 2)
        self.assertEqual(datetime.fromisoformat(rolled["reset_at"]), T0 + timedelta(hours=window_h))
        self.assertEqual(datetime.fromisoformat(rolled["window_start"]), T0)
        snap2 = UW.write_snapshot(h, now=T0, decl=decl, udir=udir)
        self.assertEqual(snap2["remaining_pct"], 95.0)
        # Several windows later, still only the new call is counted.
        now3 = T0 + timedelta(hours=25)
        far = UW.record_call(h, n=4, now=now3, decl=decl, udir=udir)
        self.assertEqual(far["count"], 4)
        self.assertEqual(datetime.fromisoformat(far["reset_at"]), T0 + timedelta(hours=30))
        self.assertEqual(datetime.fromisoformat(far["window_start"]), T0 + timedelta(hours=20))
        snap3 = UW.write_snapshot(h, now=now3, decl=decl, udir=udir)
        self.assertEqual(snap3["remaining_pct"], 90.0)
        self.assertAlmostEqual(snap3["burn_pct_per_h"], 10.0 / 5.0, places=6)
        self.assertNotEqual(far["count"], 9 + 2 + 4)
        self.assertNotEqual(far["count"], 2 + 4)

    def test_writer_does_not_touch_calls_json_or_caps(self):
        before = (self._sha(self._caps), self._sha(self._calls), os.path.isfile(self._calls))
        self.assertEqual(before, self._finger)
        with open(os.path.join(ROOT, "usage_writer.py")) as f:
            src = f.read()
        self.assertNotIn("calls.json", src)
        self.assertNotIn("caps.json", src)
        self.assertIn("os.replace", src)
        now = _wall(2026, 5, 4, 8)
        reset = (now + timedelta(hours=48)).isoformat()
        decl = {h: {"allowance": 20, "window_len_h": 24, "reset_at": reset} for h in UW.HARNESSES}
        udir = self._udir("isolated")
        for h in UW.HARNESSES:
            UW.record_call(h, n=2, now=now, decl=decl, udir=udir)
            snap = UW.write_snapshot(h, now=now, decl=decl, udir=udir)
            self.assertEqual(snap["state"], "counter")
        after = (self._sha(self._caps), self._sha(self._calls), os.path.isfile(self._calls))
        self.assertEqual(after, before)

    def test_no_local_or_fifth_harness(self):
        udir = self._udir("bad")
        before = sorted(os.listdir(udir))
        for bad in ("local", "fifth"):
            with self.assertRaises(ValueError) as rec:
                UW.record_call(bad, udir=udir)
            self.assertIn(bad, str(rec.exception))
            with self.assertRaises(ValueError) as snap:
                UW.write_snapshot(bad, udir=udir)
            self.assertIn(bad, str(snap.exception))
        self.assertEqual(sorted(os.listdir(udir)), before)
        self.assertEqual(sorted(UW.HARNESSES), ["agy-gemini", "cursor", "grok-build"])
        self.assertNotIn("local", UW.HARNESSES)
        self.assertEqual(len(UW.HARNESSES), 3)


if __name__ == "__main__":
    unittest.main()
