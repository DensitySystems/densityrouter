"""Config run_host: local vs ssh to a role-named host. Fakes only: the ssh client is a temp script that records
its argv and prints a canned reply. No real ssh, no network, no real CLI."""
import json, os, stat, subprocess, sys, tempfile, unittest
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
PY = sys.executable

def _config(tmp, mode):
    with open(os.path.join(HERE, "fixtures", "test.config")) as f: c = json.load(f)
    fake = os.path.join(tmp, "fake-ssh"); rec = os.path.join(tmp, "ssh-argv.json")
    with open(fake, "w") as f:
        f.write(f"#!{PY}\nimport json, sys\njson.dump(sys.argv[1:], open({rec!r}, 'w'))\nprint('REMOTE-OK')\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
    c["ssh"]["bin"] = fake
    for h in ("grok-build", "cursor", "agy-gemini"):
        c["harnesses"][h]["run_host"] = ({"mode": "local"} if mode == "local" else
            {"mode": "ssh", "host": "cli-host", "ssh_opts": ["-o", "BatchMode=yes", "-J", "jump-host"], "remote_cwd": "~/trusted"})
    p = os.path.join(tmp, "densityrouter.config")
    with open(p, "w") as f: json.dump(c, f)
    return p, rec

def _probe(cfg, code):
    env = dict(os.environ, DENSITYROUTER_CONFIG=cfg, PYTHONDONTWRITEBYTECODE="1",
               DISPATCH_STATE=os.path.dirname(cfg), DISPATCH_KILL=os.path.join(os.path.dirname(cfg), "KILL"))
    r = subprocess.run([PY, "-c", f"import sys; sys.path.insert(0, {ROOT!r})\n" + code], capture_output=True, text=True, env=env)
    if r.returncode: raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])

class RunHost(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.mkdtemp()
    def test_local_mode_is_identity(self):
        cfg, rec = _config(self.tmp, "local")
        out = _probe(cfg, "import json, drconfig as C, harness_call as HC\n"
                     "print(json.dumps([C.wrap('agy-gemini', ['x', '-p', 'hi']), HC.build_argv('agy-gemini', 'hi')[0]]))")
        self.assertEqual(out[0], ["x", "-p", "hi"])
        self.assertTrue(os.path.isabs(out[1]))          # local bin is expanded to an absolute path
        self.assertFalse(os.path.exists(rec))            # ssh never ran
    def test_ssh_mode_wraps_argv_and_quotes_prompt(self):
        cfg, rec = _config(self.tmp, "ssh")
        out = _probe(cfg, "import json, drconfig as C\n"
                     "print(json.dumps(C.wrap('cursor', ['~/.local/bin/cursor-agent', '-p', '--', 'a b; rm -rf x'])))")
        self.assertTrue(out[0].endswith("fake-ssh"))
        self.assertEqual(out[1:6], ["-o", "BatchMode=yes", "-J", "jump-host", "cli-host"])
        self.assertEqual(out[6], "--")
        self.assertEqual(out[7], "cd ~/trusted && ~/.local/bin/cursor-agent -p -- 'a b; rm -rf x'")
    def test_ssh_mode_call_goes_through_fake_ssh(self):
        cfg, rec = _config(self.tmp, "ssh")
        pk = os.path.join(self.tmp, "p.md")
        with open(pk, "w") as f: f.write("hello packet")
        out = _probe(cfg, f"import json, harness_call as HC\nr = HC.call('agy-gemini', packet={pk!r})\n"
                     "print(json.dumps({'status': r['status'], 'stdout': r.get('stdout', '')}))")
        self.assertEqual(out["status"], "done"); self.assertIn("REMOTE-OK", out["stdout"])
        with open(rec) as f: argv = json.load(f)
        self.assertIn("cli-host", argv); self.assertEqual(argv[-1], "cd ~/trusted && /bin/false -p 'hello packet'")
    def test_ssh_mode_tty_for_usage_reads(self):
        cfg, _ = _config(self.tmp, "ssh")
        out = _probe(cfg, "import json, drconfig as C\nprint(json.dumps(C.wrap('grok-build', ['~/.grok/bin/grok'], tty=True)))")
        self.assertIn("-t", out); self.assertLess(out.index("-t"), out.index("cli-host"))
    def test_bad_mode_rejected(self):
        cfg, _ = _config(self.tmp, "local")
        with open(cfg) as f: c = json.load(f)
        c["harnesses"]["cursor"]["run_host"] = {"mode": "ssh"}
        with open(cfg, "w") as f: json.dump(c, f)
        with self.assertRaises(AssertionError) as e: _probe(cfg, "import drconfig as C\nC.wrap('cursor', ['x'])")
        self.assertIn("needs host", str(e.exception))
if __name__ == "__main__": unittest.main()
