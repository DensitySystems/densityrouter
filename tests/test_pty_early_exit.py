"""usage_layer.pty_capture early EOF/EIO (2026-10-25 post-read finding). Fake CLIs only: python3 stubs, no model, no network."""
import os, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DENSITYROUTER_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "test.config"))
import usage_layer as U
PY = sys.executable

class PtyEarlyExit(unittest.TestCase):
    def test_cli_exits_while_signing_in_is_flagged(self):
        # Fake agy: shows "Signing in..." then exits (rc 3) before /usage can render.
        argv = [PY, "-c", "import sys,time; print('Signing in...', flush=True); time.sleep(0.3); sys.exit(3)"]
        t = time.time()
        with self.assertRaises(U.CliExitedEarly) as cm:
            U.pty_capture(argv, "/usage", boot_s=2.0, after_s=1.0)
        self.assertIn("rc 3", str(cm.exception))
        self.assertLess(time.time() - t, 4.0)
    def test_cli_drops_tty_but_stays_alive_keeps_pumping(self):
        # Fake CLI closes its tty (EIO on the master) but keeps running: no early return, full pump time is used.
        argv = [PY, "-c", "import os,time; print('Signing in...', flush=True); [os.close(i) for i in (0,1,2)]; time.sleep(30)"]
        t = time.time()
        out = U.pty_capture(argv, "/usage", boot_s=1.0, after_s=1.0)
        self.assertGreaterEqual(time.time() - t, 1.0 + 2.0 + 1.0 + 1.0 - 0.3)
        self.assertIn("Signing in", out)
    def test_healthy_cli_renders_panel(self):
        argv = [PY, "-c", "import sys,time; print('ready', flush=True); l=sys.stdin.readline(); print('USAGE PANEL', flush=True); time.sleep(30)"]
        out = U.pty_capture(argv, "/usage", boot_s=0.5, after_s=0.5)
        self.assertIn("USAGE PANEL", out)
if __name__ == "__main__": unittest.main()
