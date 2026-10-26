"""Cursor invoke path: absolute cursor-agent, -p text mode, report-only agent guard."""
import os, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness_call as HC

class CursorInvoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); os.environ["DISPATCH_STATE"] = self.tmp
        self.pkt = os.path.join(self.tmp, "p.md"); open(self.pkt, "w").write("echo CURSOR-MARKER-1")
    def test_argv_absolute_print_text(self):
        a = HC.build_argv("cursor", "hi")
        self.assertTrue(os.path.isabs(a[0])); self.assertEqual(a[0], "/bin/false")  # the test config points every CLI at /bin/false
        self.assertEqual(a[1:], ["-p", "--trust", "--output-format", "text", "--", "hi"]); self.assertIsNone(HC.forbidden(a))
        for bad in ("--force", "--yolo", "--mode"): self.assertNotIn(bad, a)
    def test_guard_ok_grok(self):
        d = tempfile.mkdtemp(); g = os.path.join(d, ".grok", "downloads"); os.makedirs(g)
        real = os.path.join(g, "grok-1.0.70-linux-x86_64"); open(real, "w").close()
        link = os.path.join(d, "agent"); os.symlink(real, link)
        self.assertTrue(HC.agent_guard(lambda n: link)["ok"])
    def test_guard_flags_cursor_shadow(self):
        d = tempfile.mkdtemp(); real = os.path.join(d, "cursor-agent"); open(real, "w").close()
        r = HC.agent_guard(lambda n: real); self.assertFalse(r["ok"]); self.assertIn("issue", r)
        self.assertFalse(HC.agent_guard(lambda n: None)["ok"])
    def test_missing_binary(self):
        old = HC.INVOKE["cursor"][0]; HC.INVOKE["cursor"][0] = "/nonexistent/cursor-agent"
        try: r = HC.call("cursor", packet=self.pkt)
        finally: HC.INVOKE["cursor"][0] = old
        self.assertEqual(r["status"], "failed_hold:not_installed")
    def test_invoke_fake_cli_returns_marker(self):
        fake = os.path.join(self.tmp, "cursor-agent")
        open(fake, "w").write('#!/bin/sh\nfor a; do last="$a"; done\necho "$last" | sed "s/^echo //"\n'); os.chmod(fake, 0o755)
        old = HC.INVOKE["cursor"][0]; HC.INVOKE["cursor"][0] = fake
        try: r = HC.call("cursor", packet=self.pkt)
        finally: HC.INVOKE["cursor"][0] = old
        self.assertEqual(r["status"], "done"); self.assertIn("CURSOR-MARKER-1", r["stdout"]); self.assertIn("agent_guard", r)
    def test_not_logged_in_reason(self):
        fake = os.path.join(self.tmp, "cursor-agent")
        open(fake, "w").write('#!/bin/sh\necho "Not logged in" >&2\nexit 1\n'); os.chmod(fake, 0o755)
        old = HC.INVOKE["cursor"][0]; HC.INVOKE["cursor"][0] = fake
        try: r = HC.call("cursor", packet=self.pkt)
        finally: HC.INVOKE["cursor"][0] = old
        self.assertEqual(r["reason"], "cursor-agent not logged in"); self.assertNotEqual(r["status"], "done")
class NoCeiling(unittest.TestCase):
    def test_cap_values(self):
        for v in (None, 0, "0", "x", -1): self.assertIsNone(HC._cap(v))
        self.assertEqual(HC._cap(2), 2)
    def test_load_caps_missing_fields(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "caps.json")
        open(p, "w").write('{"grok-build":{"enabled":true},"cursor":{"test_call_cap":null},"agy-gemini":{"test_call_cap":0}}')
        self.assertEqual(set(HC.load_caps(p).values()), {None})
    def test_no_cap_never_blocked_every_harness(self):
        tmp = tempfile.mkdtemp(); os.environ["DISPATCH_STATE"] = tmp
        pkt = os.path.join(tmp, "p.md"); open(pkt, "w").write("echo X")
        old = dict(HC.CAPS)
        try:
            for h in ("grok-build", "agy-gemini", "cursor"):
                HC.CAPS[h] = None
                for _ in range(4):
                    r = HC.call(h, argv=["/bin/echo", "ok"], packet=pkt)
                    self.assertNotIn(r["status"], ("BLOCKED", "REFUSED", "NOT_DISPATCHED")); self.assertEqual(r["status"], "done")
        finally: HC.CAPS.clear(); HC.CAPS.update(old)


class TestCursorTimeoutR5(unittest.TestCase):
    def test_cursor_timeout_180_others_60(self):
        import harness_call as H
        self.assertEqual(H.HARNESS_TIMEOUT.get("cursor"), 180)
        self.assertEqual(H.TIMEOUT, 60)
        self.assertEqual(H.HARNESS_TIMEOUT.get("codex", H.TIMEOUT), 60)

    def test_cursor_run_passes_180_and_devnull(self):
        import harness_call as H, subprocess
        from unittest import mock
        seen = {}
        def fake(argv, **kw):
            seen.update(kw); return subprocess.CompletedProcess(argv, 0, "ok", "")
        with mock.patch.object(H.subprocess, "run", side_effect=fake), mock.patch.object(H.D, "check_kill", return_value=None):
            os.environ["DISPATCH_STATE"] = tempfile.mkdtemp()
            fb = os.path.join(os.environ["DISPATCH_STATE"], "cursor-agent"); open(fb, "w").close()
            r = H.call("cursor", argv=[fb, "-p", "--trust", "--output-format", "text", "x"])
            self.assertNotEqual(r.get("status"), "REFUSED", r)
        self.assertEqual(seen.get("timeout"), 180)
        self.assertIs(seen.get("stdin"), subprocess.DEVNULL)

if __name__ == "__main__": unittest.main()
