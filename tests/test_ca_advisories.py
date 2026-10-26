"""CA advisories (REVIEW-CA-ROUTER-CURSOR-INVOKE-20261025-r1).
Cursor packet text is after `--`. forbidden() catches --force/--yolo/-f and ignores tokens after `--`.
Live calls use a scratch cwd under the state dir. An unpinned walk moves past failed_hold:not_installed.
Stdlib unittest. Temp DISPATCH_STATE only. Fake call_fn. No real harness.
Run: python3 tests/test_ca_advisories.py
"""
import os, shutil, subprocess, sys, tempfile, unittest
import unittest.mock
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, harness_call as HC, live as LV

TEXT = (
    "# PACKET: ca\n"
    "\n"
    "## Objective\n"
    "Fix a script in the scratch dir.\n"
)
WALK = {"failed_retryable", "timed_out", "failed_hold:usage", "failed_hold:not_installed"}


class CursorForbidden(unittest.TestCase):
    def test_cursor_argv_packet_after_separator(self):
        packet = "PACKET-TEXT"
        a = HC.build_argv("cursor", packet)
        self.assertEqual(a[-2:], ["--", packet])
        self.assertEqual(a[1:], ["-p", "--trust", "--output-format", "text", "--", packet])
        self.assertTrue(os.path.isabs(a[0]))
        self.assertEqual(HC.HARNESS_TIMEOUT.get("cursor"), 180)
        self.assertEqual(HC.TIMEOUT, 60)
        self.assertIsNone(HC.forbidden(a))

    def test_forbidden_flags_force_yolo_f(self):
        self.assertEqual(HC.forbidden(["bin", "--force"]), "--force")
        self.assertEqual(HC.forbidden(["bin", "--yolo"]), "--yolo")
        self.assertEqual(HC.forbidden(["bin", "-f"]), "-f")
        self.assertEqual(HC.forbidden(["bin", "--force=1"]), "--force")
        self.assertEqual(HC.forbidden(["bin", "--forc"]), "--force")  # same prefix rule as --mod
        self.assertEqual(HC.forbidden(["bin", "--YOLO"]), "--yolo")
        self.assertEqual(HC.forbidden(["bin", "-f", "--", "--force"]), "-f")
        self.assertIsNone(HC.forbidden(["bin", "-F", "-ff", "--formal", "--model", "--trust", "--output-format", "-p"]))

    def test_packet_text_after_separator_not_flagged(self):
        packet = "--force"
        argv = HC.build_argv("cursor", packet)
        self.assertEqual(argv[-2:], ["--", packet])
        self.assertIsNone(HC.forbidden(argv))
        self.assertIsNone(HC.forbidden(["bin", "-p", "--trust", "--output-format", "text", "--",
                                        "--force", "--yolo", "-f", "--force=1", "--mode"]))
        self.assertIsNone(HC.forbidden(["bin", "--", "see --force and -f in the packet"]))

    def test_current_templates_not_flagged(self):
        for h in ("grok-build", "agy-gemini", "cursor"):
            tmpl = HC.INVOKE[h]
            self.assertIsNotNone(tmpl, h)
            self.assertIsNone(HC.forbidden(tmpl), h)
            self.assertIsNone(HC.forbidden(HC.build_argv(h, "reply OK")), h)
        self.assertIsNone(HC.forbidden(HC.build_argv("cursor", "--force")))
        self.assertIn("--trust", HC.INVOKE["cursor"])
        self.assertEqual(HC.INVOKE["cursor"][-2:], ["--", HC.PROMPT])


class LiveCwdAndWalk(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL", "DISPATCH_LIVE")}
        os.environ["DISPATCH_STATE"] = self.tmp
        os.environ.pop("DISPATCH_LIVE", None)
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL")
        os.environ["DISPATCH_KILL"] = D.KILL
        self.pk = os.path.join(self.tmp, "PACKET-CA-r1.md")
        with open(self.pk, "w", encoding="utf-8") as f:
            f.write(TEXT)
        sha12 = D.sha256f(self.pk)[:12]
        self.ledger = os.path.join(self.tmp, "ledger.md")
        self.runs = os.path.join(self.tmp, "RUNS.tsv")
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write("| 1 | GO | PACKET-CA-r1.md %s |\n" % sha12)
        with open(self.runs, "w", encoding="utf-8") as f:
            f.write("x\t%s\n" % self.pk)
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = D.load(os.path.join(ROOT, "caps.json"))
        self._hc_run = HC.subprocess.run
        self._subprocess_run = subprocess.run

        def boom(*a, **k):
            raise AssertionError("subprocess.run")

        subprocess.run = boom
        HC.subprocess.run = boom
        ranked = [r["harness"] for r in D.route(TEXT, self.capmap, self.caps)["ranked"] if r["score"] is not None]
        self.assertEqual(ranked, ["grok-build", "cursor", "agy-gemini"])
        self.assertNotIn("async-fake", ranked)
        self.assertEqual(D.WALK_STATUSES, WALK)
        self.assertNotIn("NOT_DISPATCHED", D.WALK_STATUSES)

    def tearDown(self):
        subprocess.run = self._subprocess_run
        HC.subprocess.run = self._hc_run
        D.KILL = self._kill
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")

    def _live(self, call_fn, pin=None):
        env = {"DISPATCH_LIVE": "1", "DISPATCH_STATE": self.tmp}
        self.assertNotEqual(os.environ.get("DISPATCH_LIVE"), "1")
        rc, out = LV.run(self.pk, self.ledger, self.runs, self.capmap, self.caps,
                         env=env, call_fn=call_fn,
                         pin=pin)
        return env, rc, out

    def test_call_cwd_is_state_jobs_dir(self):
        seen = []

        def call_fn(h, job=None, packet=None, **k):
            seen.append((h, job, k.get("cwd")))
            return {"harness": h, "status": "done"}

        env, rc, out = self._live(call_fn)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(len(seen), 1)
        h, job, cwd = seen[0]
        self.assertEqual(h, "grok-build")
        self.assertEqual(job, out["handoff"]["job"])
        expect = os.path.join(os.path.dirname(LV.live_file(env)), "jobs", job)
        self.assertEqual(cwd, expect)
        self.assertEqual(expect, os.path.join(self.tmp, "jobs", job))
        self.assertTrue(os.path.isdir(cwd))
        self.assertNotEqual(cwd, os.getcwd())
        self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(os.getcwd()))

    def test_unpinned_not_installed_walks_pinned_exits_6(self):
        seen = []

        def call_fn(h, job=None, packet=None, **k):
            seen.append(h)
            if h == "grok-build":
                return {"harness": h, "status": "failed_hold:not_installed"}
            return {"harness": h, "status": "done"}

        _env, rc, out = self._live(call_fn)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(seen, ["grok-build", "cursor"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build", "cursor"])
        self.assertEqual(out["attempts"][0]["status"], "failed_hold:not_installed")
        self.assertEqual(out["call"]["harness"], "cursor")
        self.assertEqual(out["call"]["status"], "done")
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))

        seen.clear()

        def pinned(h, job=None, packet=None, **k):
            seen.append(h)
            return {"harness": h, "status": "failed_hold:not_installed"}

        _env, rc, out = self._live(pinned, pin="grok-build")
        self.assertEqual(rc, 6, out["handoff"]["status"])
        self.assertEqual(seen, ["grok-build"])
        self.assertEqual(out["handoff"]["walked"], ["grok-build"])
        self.assertEqual(out["call"]["status"], "failed_hold:not_installed")
        self.assertTrue(out["handoff"]["pinned"])
        self.assertGreater(len(out["route"]["ranked"]), 1)


class GrokBuildBin(unittest.TestCase):
    def test_grok_build_invoke_is_absolute(self):
        argv0 = HC.INVOKE["grok-build"][0]
        self.assertTrue(os.path.isabs(argv0))
        self.assertTrue(argv0 == "/bin/false")


class NoCapRefusal(unittest.TestCase):
    def test_no_cap_means_no_refusal(self):
        H = HC
        tmp = tempfile.mkdtemp()
        prev_state = os.environ.get("DISPATCH_STATE")
        os.environ["DISPATCH_STATE"] = tmp
        with open(os.path.join(tmp, "calls.json"), "w", encoding="utf-8") as f:
            f.write('{"grok-build": 2}')
        fake = subprocess.CompletedProcess(args=["/bin/echo", "ok"], returncode=0, stdout="ok", stderr="")
        try:
            self.assertGreaterEqual(H.used("grok-build"), 2)
            with unittest.mock.patch.dict(H.CAPS, {"grok-build": 2}, clear=True), \
                    unittest.mock.patch.object(H.subprocess, "run", return_value=fake):
                self.assertEqual(H.CAPS, {"grok-build": 2})
                self.assertGreaterEqual(H.used("grok-build"), H.CAPS["grok-build"])
                r = H.call("grok-build", argv=["/bin/echo", "ok"])
            self.assertNotEqual(r["status"], "REFUSED")
            self.assertEqual(r["status"], "done")
            self.assertEqual(r["rc"], 0)
        finally:
            if prev_state is None:
                os.environ.pop("DISPATCH_STATE", None)
            else:
                os.environ["DISPATCH_STATE"] = prev_state
            shutil.rmtree(tmp, ignore_errors=True)


class PinnedBin(unittest.TestCase):
    def test_relative_override_rejected(self):
        pinned = os.path.expanduser("~/.grok/bin/grok")
        allow = {pinned, os.path.expanduser("~/.grok/bin/grok")}
        got = HC._pinned_bin("GROK_BUILD_BIN", pinned, allow, env={"GROK_BUILD_BIN": "grok"})
        self.assertEqual(got, pinned)
        cursor = HC._CURSOR_PINNED
        callow = {cursor, os.path.expanduser("~/.local/bin/cursor-agent")}
        got = HC._pinned_bin("CURSOR_AGENT_BIN", cursor, callow, env={"CURSOR_AGENT_BIN": "cursor-agent"})
        self.assertEqual(got, cursor)
        self.assertEqual(HC.INVOKE["cursor"][1:], ["-p", "--trust", "--output-format", "text", "--", HC.PROMPT])
        self.assertEqual(HC.HARNESS_TIMEOUT.get("cursor"), 180)
        self.assertEqual(HC.TIMEOUT, 60)

    def test_arbitrary_absolute_rejected(self):
        pinned = os.path.expanduser("~/.grok/bin/grok")
        allow = {pinned, os.path.expanduser("~/.grok/bin/grok")}
        got = HC._pinned_bin("GROK_BUILD_BIN", pinned, allow, env={"GROK_BUILD_BIN": "/bin/sh"})
        self.assertEqual(got, pinned)
        cursor = HC._CURSOR_PINNED
        got = HC._pinned_bin(
            "CURSOR_AGENT_BIN", cursor,
            {cursor, os.path.expanduser("~/.local/bin/cursor-agent")},
            env={"CURSOR_AGENT_BIN": "/bin/sh"})
        self.assertEqual(got, cursor)

    def test_allowlisted_accepted(self):
        d = tempfile.mkdtemp()
        try:
            allowed = os.path.join(d, "grok")
            with open(allowed, "w", encoding="utf-8") as f:
                f.write("")
            pinned = os.path.join(d, "pinned-grok")
            got = HC._pinned_bin("GROK_BUILD_BIN", pinned, {allowed}, env={"GROK_BUILD_BIN": allowed})
            self.assertEqual(got, allowed)
            self.assertNotEqual(got, pinned)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_symlink_to_bin_sh_rejected(self):
        d = tempfile.mkdtemp()
        try:
            allowed = os.path.join(d, "allowed-bin")
            with open(allowed, "w", encoding="utf-8") as f:
                f.write("")
            link = os.path.join(d, "via-sh")
            os.symlink("/bin/sh", link)
            pinned = os.path.join(d, "pinned")
            got = HC._pinned_bin("CURSOR_AGENT_BIN", pinned, {allowed}, env={"CURSOR_AGENT_BIN": link})
            self.assertEqual(got, pinned)
            self.assertNotEqual(os.path.realpath(link), os.path.realpath(allowed))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
