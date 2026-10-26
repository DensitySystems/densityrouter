import re
"""Usage layer. Fake captures and runners, temp ledger only."""
import json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import usage_layer as U

NOW = datetime(2026, 10, 25, 14, 59).astimezone()
GROK = "Weekly limit (SuperGrok Heavy)\n███░░░\n%s%%\nResets: October 29, 01:56\n"
CURSOR = " Usage • Ultra                                  Resets Nov 13\n Included        %s%% used             ███\n   Auto          3%% used\n   API           0%% used\n On-Demand       $0 / $10\n"
AGY = ("GEMINI MODELS\n  Weekly Limit Remaining\n[██] 99.99%%\nRefreshes in 77h 19m\n  Five Hour Limit Remaining\n[██] 100.00%%\n"
       "GEMINI MODEL X\n  Gemini Model X Weekly Limit\n[██░] %s%%\nRefreshes in 124h 43m\n")

def reader_seq(texts, h):
    it = iter(texts)
    return lambda x: U.read_usage(x, capture=lambda argv, keys: next(it), now=NOW)

class UsageLayer(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.ledger = os.path.join(self.d, "ledger.jsonl")
        os.environ.pop("DISPATCH_LIVE", None)

    def test_parse_grok_used_and_reset_mt(self):
        p = U.parse_grok(GROK % 9, NOW)["weekly"]
        self.assertEqual((p["used_pct"], p["remaining_pct"]), (9.0, 91.0))
        self.assertTrue(p["reset_at"].startswith("2026-10-29T01:56:00"))
    def test_parse_cursor_included_no_reset_printed(self):
        p = U.parse_cursor(CURSOR % 3, NOW)
        self.assertEqual(p["included"]["remaining_pct"], 97.0); self.assertEqual(p["included"]["reset_at"], "2026-11-13")
        self.assertEqual(U.parse_cursor(" Included        3% used\n", NOW)["included"]["reset_at"], "unknown")
        self.assertEqual(p["on_demand"]["limit_usd"], 10.0)
    def test_parse_agy_groups_and_refresh(self):
        p = U.parse_agy(AGY % "83.84", NOW)
        self.assertEqual(p["gemini_model_x.weekly"]["remaining_pct"], 83.84)
        self.assertEqual(p["gemini_models.weekly"]["resets_in"], "77h 19m")
        self.assertEqual(p["gemini_models.five_hour"]["reset_at"], "unknown")

    def test_unreadable_screen_is_flagged_not_zero(self):
        r = U.read_usage("grok-build", capture=lambda a, k: "garbage", now=NOW)
        self.assertFalse(r["parse_ok"]); self.assertEqual(r["state"], "parse_failed"); self.assertIsNone(r["primary"])
    def test_no_cli_pool_is_unknown(self):
        for h in ("x",):
            r = U.read_usage(h, now=NOW); self.assertFalse(r["parse_ok"]); self.assertEqual(r["state"], "unknown")
    def test_read_error_flagged(self):
        def boom(a, k): raise OSError("no tty")
        r = U.read_usage("cursor", capture=boom, now=NOW)
        self.assertEqual((r["parse_ok"], r["state"]), (False, "read_error"))

    def test_run_request_row_pre_post_delta(self):
        row = U.run_request("agy-gemini", "this is only a test", "bot-1", "req-1",
                            reader=reader_seq([AGY % "79.51", AGY % "79.41"], "agy-gemini"),
                            runner=lambda h, p: (0, "ok"), ledger=self.ledger)
        self.assertTrue(row["parse_ok"]); self.assertIsNone(row["flagged"])
        self.assertAlmostEqual(row["delta"]["gemini_model_x.weekly"]["used_pct"], 0.1, places=6)
        self.assertEqual(row["tokens"]["input"], "unknown")      # agy prints no tokens: unknown, never 0
        rows = U.read_ledger(self.ledger); self.assertEqual(len(rows), 1)
        for k in ("ts", "request_id", "bot_id", "harness", "model", "tokens", "pre", "post", "delta", "parse_ok"):
            self.assertIn(k, rows[0])
    def test_unreadable_run_flagged_delta_unknown(self):
        row = U.run_request("grok-build", "x", "bot-1", "req-2", reader=reader_seq(["", GROK % 9], "grok-build"),
                            runner=lambda h, p: (0, "ok"), token_extra={}, ledger=self.ledger)
        self.assertFalse(row["parse_ok"]); self.assertEqual(row["delta"], "unknown"); self.assertTrue(row["flagged"])
    def test_cursor_json_tokens_scraped(self):
        out = json.dumps({"type": "result", "usage": {"inputTokens": 11, "outputTokens": 2, "cacheReadTokens": 3}})
        t, m = U.scrape_tokens("cursor", out)
        self.assertEqual((t["input"], t["output"], t["cached"], t["reasoning"]), (11, 2, 3, "unknown"))
    def test_grok_session_tokens_scraped(self):
        t, m = U.scrape_tokens("grok-build", "", {"session": {"inputTokens": 5, "outputTokens": 1, "cachedReadTokens": 0,
                                                              "reasoningTokens": 2, "costUsdTicks": 9, "primaryModelId": "m"}})
        self.assertEqual((t["input"], t["reasoning"], t["cost"], m), (5, 2, U.ticks_cost(9), "m"))

    def _row(self, pool, used, ts=None, ok=True, tokens=None):
        prim = "p"
        return {"ts": (ts or NOW).isoformat(), "pool": pool, "parse_ok": ok, "tokens": tokens or {"input": 10},
                "post": {"primary": prim, "read_at": NOW.isoformat(), "pools": {prim: {"used_pct": used, "reset_at": "r"}}}}
    def test_pools_are_separate_no_shared_pool(self):
        self.assertEqual(set(U.POOLS), {"grok-build", "cursor", "agy-gemini"})
        r = U.rollup([self._row("grok-build", 91.0), self._row("cursor", 10.0)], "weekly", NOW)
        self.assertEqual(r["pools"]["grok-build"]["alert"], 90.0); self.assertEqual(r["pools"]["cursor"]["alert"], None)
        self.assertEqual(r["pools"]["agy-gemini"]["used_pct"], "unknown")
    def test_async_fake_not_in_usage_layer(self):  # async-fake is tracked from its own logs, not the router
        self.assertNotIn("async-fake", U.POOLS); self.assertNotIn("async-fake", U.HARNESS_POOL)
        self.assertNotIn("async-fake", U.PARSERS); self.assertNotIn("async-fake", U.REQUEST_ARGV)
    def test_alert_levels(self):
        self.assertEqual([U.alert_level(v) for v in (10, 75, 89.9, 90, "unknown")], [None, 75.0, 75.0, 90.0, "unknown"])
    def test_alert_writes_log_line_only(self):
        log = os.path.join(self.d, "alerts.log")
        self.assertIsNone(U.alert_line("cursor", 50.0, log=log)); self.assertIsNone(U.alert_line("cursor", "unknown", log=log))
        self.assertIn("level=90%", U.alert_line("cursor", 91.0, log=log))
        with open(log) as f: self.assertEqual(len(f.read().splitlines()), 1)
    def test_no_throttle_in_this_build(self):
        self.assertIsNone(U.THROTTLE_PCT)
        self.assertFalse(hasattr(U, "should_throttle"))
        U.append_row(self._row("agy-gemini", 99.0), self.ledger)
        called = []
        row = U.run_request("agy-gemini", "x", "bot-2", "req-3", reader=reader_seq([AGY % "1.00", AGY % "0.90"], "agy-gemini"),
                            runner=lambda h, p: (called.append(p), (0, "ok"))[1], ledger=self.ledger, critical=False)
        self.assertEqual(called, ["x"]); self.assertNotIn("event", row)
        self.assertTrue(os.path.exists(os.path.join(self.d, "usage-alerts.log")))
    def test_reply_stored_as_hash_and_length(self):
        row = U.run_request("agy-gemini", "x", "b", "r", reader=reader_seq([AGY % "50", AGY % "50"], "agy-gemini"),
                            runner=lambda h, p: (0, "TOKEN=abc123 reply"), ledger=self.ledger)
        self.assertNotIn("abc123", json.dumps(row)); self.assertEqual(row["reply"]["len"], 18)
        self.assertEqual(len(row["reply"]["sha256"]), 64); self.assertNotIn("reply_excerpt", row)
    def test_grok_parser_anchored(self):
        decoy = "Session usage 45%\nWeekly limit (SuperGrok Heavy)\n███░░░\n9%\nResets: October 29, 01:56\nModel share 77%\n"
        self.assertEqual(U.parse_grok(decoy, NOW)["weekly"]["used_pct"], 9.0)
        self.assertEqual(U.parse_grok("Weekly limit (X)\n12 tokens 80% of\n", NOW), {})
    def test_request_usage_exact_vs_coarse(self):
        tok = {"input": 10, "output": 2, "cached": 0, "reasoning": "unknown", "cost": "unknown"}
        pre = U.read_usage("cursor", capture=lambda a, k: CURSOR % 3, now=NOW)
        r = U.make_row("q", "b", "cursor", "m", 0, 0, 1, tok, pre, pre, True, "")
        self.assertEqual(r["request_usage"], {"basis": "printed_tokens", "exact": True, "tokens": 12})
        a1 = U.read_usage("agy-gemini", capture=lambda a, k: AGY % "79.51", now=NOW)
        a2 = U.read_usage("agy-gemini", capture=lambda a, k: AGY % "79.41", now=NOW)
        r = U.make_row("q", "b", "agy-gemini", "m", 0, 0, 1, {k: "unknown" for k in U.TOKEN_KEYS}, a1, a2, True, "")
        self.assertTrue(r["request_usage"]["coarse"]); self.assertFalse(r["request_usage"]["exact"])
        self.assertAlmostEqual(r["request_usage"]["used_pct_delta"], 0.1, places=6)
    def _brow(self, used0, used1, tin, tout):
        mk = lambda u: {"primary": "included", "pools": {"included": {"used_pct": u}}}
        return {"pool": "cursor", "harness": "cursor", "parse_ok": True, "pre": mk(used0), "post": mk(used1),
                "tokens": {"input": tin, "output": tout}}
    def test_batch_match_fixtures(self):
        rows = [self._brow(3, 3, 400000, 0), self._brow(3, 4, 300000, 300000), self._brow(4, 5, 500000, 500000)]
        m = U.batch_match(rows, "cursor", tokens_per_pct=1000000)        # 2.0M tokens vs +2% -> match
        self.assertEqual((m["status"], m["flagged"], m["printed_tokens"]), ("match", None, 2000000))
        bad = U.batch_match(rows, "cursor", tokens_per_pct=400000)       # expects 5% vs 2% -> gap 3 > step 1
        self.assertEqual(bad["status"], "mismatch"); self.assertTrue(bad["flagged"])
        unk = U.batch_match(rows, "cursor")                                # pool size UNKNOWN -> coarse, never a guess
        self.assertEqual((unk["status"], unk["flagged"], unk["pool_size"]), ("coarse", None, "unknown")); self.assertNotIn("expected_pct", unk)
        rows[1]["tokens"]["output"] = "unknown"
        self.assertEqual(U.batch_match(rows, "cursor", 1000000)["status"], "coarse")
    def test_run_group_kills_process_group_on_timeout(self):
        import time
        t = time.time()
        rc, out = U.run_group(["/bin/sh", "-c", "sleep 30 & sleep 30; echo late"], timeout=1)
        self.assertEqual(rc, "timeout"); self.assertLess(time.time() - t, 10)
    def test_trust_prompt_never_answered(self):
        r = U.read_usage("agy-gemini", capture=lambda a, k: (_ for _ in ()).throw(U.TrustPromptRefused("x")), now=NOW)
        self.assertEqual((r["parse_ok"], r["state"]), (False, "read_error"))
        self.assertTrue(U.TRUST_RE.search("Do you trust this folder?"))
    def test_reset_parsed_into_every_row_with_tz_source(self):
        g = U.run_request("grok-build", "x", "b", "rg", reader=reader_seq([GROK % 9, GROK % 9], "grok-build"),
                          runner=lambda h, p: (0, "ok"), token_extra={}, ledger=self.ledger)
        self.assertTrue(g["reset"]["at"].startswith("2026-10-29T01:56:00")); self.assertIn("local zone", g["reset"]["tz_source"])
        self.assertEqual(g["reset"]["utc_offset"], g["reset"]["at"][-6:])
        c = U.run_request("cursor", "x", "b", "rc", reader=reader_seq([CURSOR % 3, CURSOR % 3], "cursor"),
                          runner=lambda h, p: (0, "{}"), ledger=self.ledger)
        self.assertEqual((c["reset"]["at"], c["reset"]["precision"], c["reset"]["utc_offset"]), ("2026-11-13T00:00:00+00:00", "date-only", "+00:00"))
        self.assertEqual(c["reset"]["date_printed"], "2026-11-13")
        self.assertEqual(U.latest_reset("cursor", self.ledger), (True, "2026-11-13T00:00:00+00:00"))
        self.assertEqual(U.latest_reset("agy-gemini", self.ledger), (False, None))
    def _waste(self, h, ledger_rows):
        import dispatcher as D
        udir = os.path.join(self.d, "u-" + h); os.makedirs(udir, exist_ok=True)
        now = datetime.now().astimezone()
        snap = {"harness": h, "remaining_pct": 80, "window_len_h": 168, "burn_pct_per_h": 0.1,
                "reset_at": (now + timedelta(hours=150)).isoformat(), "read_at": now.isoformat()}
        with open(os.path.join(udir, h + ".json"), "w") as f: json.dump(snap, f)
        os.environ["USAGE_LEDGER"] = self.ledger
        try:
            for r in ledger_rows: U.append_row(r, self.ledger)
            return D.usage_for(h, udir, now=now.timestamp()), now
        finally:
            os.environ.pop("USAGE_LEDGER", None)
    def test_waste_uses_cli_parsed_reset_not_declared(self):
        (u0, st), now = self._waste("grok-build", [])
        near = (now + timedelta(hours=2)).isoformat()
        (u1, st1), _ = self._waste("grok-build", [{"harness": "grok-build", "parse_ok": True, "reset": {"at": near}}])
        self.assertGreater(u1, u0)                      # CLI says reset in 2h: prox near 1, more waste than declared 150h
        self.assertAlmostEqual(u1, 0.8 * (1 + 0.2224 + (0.8 - 0.001 * 2) * (1 - 2 / 168)), places=3)
    def test_no_printed_reset_scores_waste_zero(self):
        (u, st), _ = self._waste("cursor", [{"harness": "cursor", "parse_ok": True, "reset": {"at": "unknown"}}])
        self.assertAlmostEqual(u, 0.8, places=6)      # U = rem: waste term 0
    def test_date_only_reset_scores_waste_zero(self):
        (u, st), _ = self._waste("cursor", [{"harness": "cursor", "parse_ok": True, "reset": {"at": "2026-11-13"}}])
        self.assertAlmostEqual(u, 0.8, places=6)
    def test_grok_session_in_out_usd_parsed_and_delta(self):  # real screen shape
        pre = GROK % 9 + "Session usage: no model calls yet in this session.\n"
        post = GROK % 9 + ("Session usage (since start or last resume):\nInput\ntokens:\n17,305\n(1,664\ncached)\nOutput\n62\n(37\n"
                           "reasoning)\nTotal\n17,367\nModel\ncalls:\n1\n\u00b7\nAPI\ntime:\n2.0s\nCost:\n$0.0110\n")
        a = U.read_usage("grok-build", capture=lambda x, k: pre, now=NOW); b = U.read_usage("grok-build", capture=lambda x, k: post, now=NOW)
        self.assertEqual(b["session"]["input"], 17305); self.assertEqual(b["session"]["cost_usd"], 0.011)
        self.assertEqual(U.session_delta(a, b), {"input": 17305, "output": 62, "cost_usd": 0.011})
        r = U.make_row("q", "b", "grok-build", "m", 0, 0, 1, {k: "unknown" for k in U.TOKEN_KEYS}, a, b, True, "")
        self.assertEqual((r["request_usage"]["basis"], r["request_usage"]["tokens"]), ("usage_screen_in_out_usd_delta", 17367))
    def test_in_out_usd_unknown_where_not_shown(self):
        self.assertIsNone(U.read_usage("cursor", capture=lambda x, k: CURSOR % 3, now=NOW)["session"])
        self.assertIsNone(U.read_usage("grok-build", capture=lambda x, k: GROK % 9, now=NOW)["session"])
        self.assertEqual(U.session_delta({}, {}), {"input": "unknown", "output": "unknown", "cost_usd": "unknown"})
    def test_daily_vs_weekly_and_unknown_tokens_counted(self):
        old = NOW - timedelta(days=2) if NOW.weekday() >= 2 else NOW
        rows = [self._row("grok-build", 9.0, ts=NOW, tokens={"input": 100, "output": "unknown"}), self._row("grok-build", 9.0, ts=old)]
        d = U.rollup(rows, "daily", NOW)["pools"]["grok-build"]; w = U.rollup(rows, "weekly", NOW)["pools"]["grok-build"]
        self.assertEqual(d["tokens_unknown_rows"]["output"], 1 if old != NOW else 2)
        self.assertGreaterEqual(w["requests"], d["requests"])
    def test_date_only_reset_as_utc_midnight_scores_waste(self):
        at = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00+00:00")
        (u, st), _ = self._waste("cursor", [{"harness": "cursor", "parse_ok": True, "reset": {"at": at, "precision": "date-only"}}])
        self.assertGreater(u, 0.8)
    def test_ticks_scale_inferred_flag(self):
        c = U.ticks_cost(110452400)
        self.assertEqual((c["usd"], c["scale_flag"]), (0.011045, "INFERRED"))
        t, _ = U.scrape_tokens("grok-build", "", {"session": {"costUsdTicks": 110452400}})
        self.assertEqual(t["cost"]["scale_flag"], "INFERRED")
    def _fake_tui(self, die_after=None):
        src = ("import sys,os\nn=0\nos.write(1,b'ready\\r\\n')\nwhile True:\n l=sys.stdin.readline()\n if not l: break\n"
               " if 'Q' in l:\n  n+=1\n  os.write(1,b'reply line\\r\\nWorked for 1s\\r\\n')\n"
               + ("  if n>=%d: os._exit(0)\n" % die_after if die_after else ""))
        return U.GrokSession(argv=["/usr/bin/python3", "-c", src], boot_s=0.5)
    def test_grok_persistent_session_reused(self):
        s = self._fake_tui()
        try:
            rc, txt, rs = U.grok_session_call(s, "Q1", timeout=5); pid = s.pid
            rc2, txt2, rs2 = U.grok_session_call(s, "Q2", timeout=5)
            self.assertEqual((rc, rc2, rs, rs2, s.pid, s.restarts), (0, 0, False, False, pid, 0))
            self.assertIn("reply line", txt)
        finally:
            s.close()
    def test_grok_session_died_restarted_and_flagged(self):  # restart, flag not-exact, never 0
        s = self._fake_tui(die_after=1)
        try:
            U.grok_session_call(s, "Q1", timeout=5)
            import time; time.sleep(0.5)
            rc, txt, restarted = U.grok_session_call(s, "Q2", timeout=5)
            self.assertTrue(restarted); self.assertEqual(s.restarts, 1); self.assertEqual(rc, 0)
        finally:
            s.close()
    def test_trust_rechecked_after_keys(self):
        import inspect
        src = inspect.getsource(U.pty_capture)
        self.assertLess(src.index("pump(2.0)"), src.index("after keys; Enter not sent"))
    def test_grok_parser_session_panel_borders(self):  # e2e: live-session /usage puts TUI border lines between header and %
        t = "Weekly limit (SuperGrok Heavy)\n\u2502\n\u2502\n\u2502\n\u2502\n\u2588\u2591\n9%\n\u2502\nResets: October 29, 01:56\n"
        self.assertEqual(U.parse_grok(t, NOW)["weekly"]["used_pct"], 9.0)
        self.assertEqual(U.parse_grok("Weekly limit (P)\nweek 3 of 4\n50%\n", NOW), {})  # a digit line between is not skipped
    def test_clean_tui_reply_strips_noise(self):  # 
        raw = ("Read the file and reply.\nBasis: note 1.\n \u2834 \nGrok Build \u2502 caps   where missing\n\n \u2826 live lease blocks reroute .\n"
               " Worked for 23s \n x shortcuts")
        self.assertEqual(U.clean_tui_reply(raw, "Read the file and reply.\nBasis: note 1.\n"),
                         "Grok Build caps where missing\nlive lease blocks reroute.")
        self.assertEqual(U.clean_tui_reply(""), "")
    def test_clean_tui_reply_keeps_small_numbers_and_line_breaks(self):
        raw = "Q?\n1. Step 1 of 3 \u2826\n2. Read 12 lines\n\n7\n\u2502 99 rows \u2502\nWorked for 4s"
        out = U.clean_tui_reply(raw, "Q?")
        self.assertEqual(out, "1. Step 1 of 3\n2. Read 12 lines\n7\n99 rows")
        for n in ("1", "3", "12", "7", "99"): self.assertIn(n, out.split())
        self.assertEqual(out.count("\n"), 3)
    def test_reap_cwd_kills_only_new_own_procs_in_dir(self):  # (g)
        import subprocess, time
        d = tempfile.mkdtemp(); t = time.time() - 1
        p = subprocess.Popen(["sleep", "30"], cwd=d)
        q = subprocess.Popen(["sleep", "30"], cwd=self.d)
        try:
            time.sleep(0.3)
            self.assertEqual(U.reap_cwd(d, since=t), [p.pid]); p.wait(5)
            self.assertIsNone(q.poll())
            self.assertEqual(U.reap_cwd(self.d, since=time.time() + 60), [])  # older than `since`: left alone
        finally:
            for x in (p, q):
                if x.poll() is None: x.kill(); x.wait()
    def test_reap_cwd_never_kills_caller_or_parent(self):  # guard against killing its own caller
        import subprocess, time
        d = tempfile.mkdtemp(); here = os.getcwd(); t = time.time() - 3600
        os.chdir(d)
        try:
            p = subprocess.Popen(["sleep", "30"], cwd=d)
            time.sleep(0.3)
            killed = U.reap_cwd(d, since=t, sig=0)  # signal 0: probe only, nothing dies
            self.assertNotIn(os.getpid(), killed); self.assertNotIn(os.getppid(), killed); self.assertIn(p.pid, killed)
            self.assertEqual(U.reap_cwd(d, since=t, sig=0, keep=[p.pid]), [])  # caller-protected pid is spared too
            self.assertEqual(U.reap_cwd(d, since=t), [p.pid]); p.wait(5)
            self.assertIsNone(os.kill(os.getpid(), 0))  # the caller (cwd == d, started before `since`... and after) is alive
        finally:
            os.chdir(here)
            if p.poll() is None: p.kill(); p.wait()
    def test_failed_session_read_not_saved(self):  # (a)
        class S: last_reading = {"parse_ok": True, "tag": "good"}
        s = S(); s.capture_usage = lambda a, k: "garbage"
        r = U.read_session_usage(s)
        self.assertFalse(r["parse_ok"]); self.assertEqual(s.last_reading["tag"], "good")
    def test_fenced_session_reader_cannot_type(self):  # (b)
        s = U.GrokSession(argv=["/bin/cat"], boot_s=0.2)
        s.start(); s.fenced = True
        s.ensure = lambda: s
        with self.assertRaises(U.SessionDied): s.capture_usage()
        s.fenced = False; s.close()
    def test_reader_cwd_is_trusted_dir(self):  # (c)
        self.assertEqual(U.reader_cwd("cursor", "/x"), "/x")
        self.assertIn(U.reader_cwd("grok-build"), (None,))
        self.assertEqual(U.READER_CWD["cursor"], U.C.expand(U.C.harness("cursor")["usage"]["reader_cwd"])); self.assertEqual(U.READER_CWD["agy-gemini"], U.C.expand(U.C.harness("agy-gemini")["usage"]["reader_cwd"]))
    def test_grok_usage_targets_verified_session_and_request_turns(self):  # (e)
        sid = "00000000-0000-7000-8000-000000000001"; calls = []
        class P:
            def __init__(self, out): self.stdout = out
        def run(argv, **k):
            calls.append((argv[1:], k.get("cwd")))
            if argv[1] == "sessions":
                return P("SESSION ID  CREATED\n%s  2026-10-25  2026-10-25  local  x\n00000000-0000-7000-8000-000000000002  old\n" % sid)
            return P(json.dumps({"sessionId": sid, "updatedAt": "2026-10-25T22:00:18.804674370+00:00", "session": {"inputTokens": 99},
                     "turns": [{"turnNumber": 1, "endedAt": "2026-10-25T21:00:00Z", "inputTokens": 5, "outputTokens": 1},
                               {"turnNumber": 2, "endedAt": "2026-10-25T22:00:18.80+00:00", "inputTokens": 65818, "outputTokens": 1876,
                                "costUsdTicks": 266492000, "primaryModelId": "grok-4.7-build"}]}))
        t0 = datetime(2026, 10, 25, 21, 59, tzinfo=timezone.utc).timestamp()
        j = U.grok_session_usage(run=run, after_ts=t0, cwd="/sess/cwd")
        self.assertEqual(j["verified_session_id"], sid)
        self.assertEqual((j["request"]["inputTokens"], j["request"]["outputTokens"], j["request"]["turns"]), (65818, 1876, [2]))
        self.assertTrue(all(c == "/sess/cwd" for _, c in calls))
        t, m = U.scrape_tokens("grok-build", "", j)
        self.assertEqual((t["input"], t["output"], t["cost"]["usd"], m), (65818, 1876, 0.026649, "grok-4.7-build"))
        def bad(argv, **k):
            return P("%s x\n" % sid) if argv[1] == "sessions" else P(json.dumps({"sessionId": "other"}))
        self.assertIsNone(U.grok_session_usage(run=bad, after_ts=t0))
    def test_grok_session_reply_clean_last_assistant(self):
        import tempfile
        from urllib.parse import quote
        root = tempfile.mkdtemp(); sid = "00000000-0000-7000-8000-000000000003"; cwd = tempfile.mkdtemp()
        d = os.path.join(root, quote(os.path.realpath(cwd), safe=""), sid); os.makedirs(d)
        rows = [{"type": "user", "content": [{"type": "text", "text": "old"}]}, {"type": "assistant", "content": "old reply"},
                {"type": "user", "content": [{"type": "text", "text": "# PACKET"}]},
                {"type": "assistant", "content": "I'll read it.", "tool_calls": [{"name": "read_file"}]},
                {"type": "tool_result", "content": "1→# README"}, {"type": "reasoning", "summary": []},
                {"type": "assistant", "content": "Line one.\n\nLine two.\nLine three."}]
        open(os.path.join(d, "chat_history.jsonl"), "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
        r = U.grok_session_reply(sid, cwd, root=root)
        self.assertEqual(r, "Line one.\n\nLine two.\nLine three.")
        self.assertFalse(re.search(r"[\u2800-\u28ff\u2500-\u259f\u25c6\u25c8\u276f]|Responding|Thinking|Ctrl\+", r))
        self.assertIsNone(U.grok_session_reply("../etc", cwd, root=root))
        self.assertIsNone(U.grok_session_reply(sid, "/nonexistent", root=root))
    def test_no_live_flag_touched(self):
        self.assertNotIn("DISPATCH_LIVE", os.environ)
        self.assertFalse(os.path.exists(os.path.join(self.d, "LIVE")))

if __name__ == "__main__": unittest.main()
