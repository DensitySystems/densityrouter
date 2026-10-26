"""Regression guard for the 2026-10-25 incident: the suite made 4 real grok calls on a CLI host (fixed in a67d483).
Runs every other tests/test_*.py with HOME set to a temp dir and PATH led by a temp dir. Trap binaries sit at
the real CLI paths (~/.grok/bin/grok, ~/.local/bin/cursor-agent, ~/.local/bin/agy) and on PATH (grok,
cursor-agent, agy, ssh). Each trap only logs its argv and exits 97. Pass = every suite OK and zero trap hits."""
import glob, os, shutil, stat, subprocess, sys, tempfile, unittest
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
TRAP = '#!/bin/sh\necho "$0 $*" >> "%s"\nexit 97\n'

class NoRealCli(unittest.TestCase):
    def test_suite_never_invokes_a_cli(self):
        t = tempfile.mkdtemp(); log = os.path.join(t, "trap.log")
        try:
            home = os.path.join(t, "home"); pbin = os.path.join(t, "pathbin")
            paths = [os.path.join(home, p) for p in (".grok/bin/grok", ".local/bin/cursor-agent", ".local/bin/agy")]
            paths += [os.path.join(pbin, n) for n in ("grok", "cursor-agent", "agy", "ssh")]
            for p in paths:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f: f.write(TRAP % log)
                os.chmod(p, stat.S_IRWXU)
            env = {k: v for k, v in os.environ.items() if k not in ("DISPATCH_LIVE", "DENSITYROUTER_CONFIG")}
            env.update(HOME=home, PATH=pbin + os.pathsep + env.get("PATH", "/usr/bin:/bin"), PYTHONDONTWRITEBYTECODE="1")
            me = os.path.abspath(__file__); fails = []
            for s in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
                if os.path.abspath(s) == me: continue
                r = subprocess.run([sys.executable, s], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
                if r.returncode != 0: fails.append((os.path.basename(s), r.stderr[-400:]))
            hits = open(log).read() if os.path.exists(log) else ""
            self.assertEqual(hits, "", "REAL CLI path invoked by the suite:\n" + hits)
            self.assertEqual(fails, [], "suites failed under trap HOME/PATH")
        finally:
            shutil.rmtree(t, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
