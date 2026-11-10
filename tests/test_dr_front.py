"""dr-front: claim, detach, at-most-once, subscribe. No network and no real CLI."""
import json, os, shutil, signal, sqlite3, subprocess, sys, tempfile, textwrap, time, unittest
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ["DENSITYROUTER_CONFIG"] = os.path.join(HERE, "fixtures", "test.config")
os.environ.setdefault("DR_FRONT_JOURNAL", "0")
sys.path.insert(0, ROOT)
import dr_front as F
import drconfig as C

PACKET = "Objective: fix the script.\n"
JOB = "0123456789abcdef"
JOB2 = "fedcba9876543210"


def _sha(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


class FrontTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.ledger = os.path.join(self.tmp, "GO.md")
        self.runs = os.path.join(self.tmp, "RUNS.tsv")
        open(self.ledger, "w").write("")
        open(self.runs, "w").write("")
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(os.path.join(self.proc, "1"))
        open(os.path.join(self.proc, "1", "cmdline"), "wb").write(b"python\x00-c\x00ok")
        self.lockdir = os.path.join(self.tmp, "run")
        os.makedirs(self.lockdir)
        self.etc = os.path.join(self.tmp, "etc")
        os.makedirs(self.etc)
        self.spawn_log = os.path.join(self.tmp, "spawns")
        self.mock = os.path.join(self.tmp, "systemd-run")
        open(self.mock, "w").write(textwrap.dedent("""\
            #!/usr/bin/env python3
            import os, sys, subprocess
            i = sys.argv.index("--")
            subprocess.Popen(sys.argv[i+1:], start_new_session=True, env=os.environ.copy(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open(os.environ["DR_FRONT_SPAWN_LOG"], "a").write("1\\n")
            raise SystemExit(0)
        """))
        os.chmod(self.mock, 0o755)
        cfg = json.load(open(os.path.join(HERE, "fixtures", "test.config")))
        cfg["paths"]["state_dir"] = self.state
        cfg["paths"]["go_ledger"] = self.ledger
        cfg["paths"]["runs"] = self.runs
        cfg["decision"]["backends_verified"] = True
        cfg["routing"] = {}
        self.cfg = os.path.join(self.tmp, "densityrouter.config")
        json.dump(cfg, open(self.cfg, "w"))
        self._env = {k: os.environ.get(k) for k in (
            "DENSITYROUTER_CONFIG", "DISPATCH_STATE", "DR_FRONT_JOURNAL", "DR_FRONT_PROC",
            "DR_FRONT_LOCK_DIR", "DR_FRONT_ETC", "DR_FRONT_SYSTEMD_RUN", "DR_FRONT_FAKE_LIVE",
            "DR_FRONT_FAKE_DELAY", "DR_FRONT_SPAWN_LOG", "DR_FRONT_CALLER_FPR", "DISPATCH_LIVE")}
        os.environ["DENSITYROUTER_CONFIG"] = self.cfg
        os.environ["DISPATCH_STATE"] = self.state
        os.environ["DR_FRONT_JOURNAL"] = "0"
        os.environ["DR_FRONT_PROC"] = self.proc
        os.environ["DR_FRONT_LOCK_DIR"] = self.lockdir
        os.environ["DR_FRONT_ETC"] = self.etc
        os.environ["DR_FRONT_SYSTEMD_RUN"] = self.mock
        os.environ["DR_FRONT_FAKE_LIVE"] = "1"
        os.environ["DR_FRONT_FAKE_DELAY"] = "0"
        os.environ["DR_FRONT_SPAWN_LOG"] = self.spawn_log
        os.environ["DR_FRONT_CALLER_FPR"] = "local"
        os.environ.pop("DISPATCH_LIVE", None)
        open(os.path.join(self.state, "LIVE"), "w").close()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _approve(self, text):
        digest = _sha(text)
        name = digest + ".md"
        with open(self.ledger, "a") as f:
            f.write("| 1 | GO | %s %s |\n" % (name, digest[:12]))
        with open(self.runs, "a") as f:
            f.write("x\t%s\n" % name)
        return digest

    def _body(self, text, job, **extra):
        import base64
        digest = self._approve(text)
        body = {"schema": 1, "call": "dispatch", "job_id": job, "intent": "start", "turn": 1,
                "packet_b64": base64.standard_b64encode(text.encode()).decode(),
                "packet_sha256": digest}
        body.update(extra)
        return body

    def _run(self, body):
        return F.handle(body, proc_root=self.proc)

    def _terminal_count(self, job):
        con = sqlite3.connect(os.path.join(self.state, "router.db"))
        n = con.execute("SELECT COUNT(*) FROM events WHERE job_id=? AND consumes=1", (job,)).fetchone()[0]
        con.close()
        return n

    def _wait_terminal(self, job, seconds=3):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._terminal_count(job):
                return True
            time.sleep(0.05)
        return False

    def test_routing_must_be_empty(self):
        with self.assertRaises(C.ConfigInvalid):
            C.routing_slot({"routing": {"profile": "density"}})
        self.assertEqual(C.routing_slot({"routing": {}}), {})
        with self.assertRaises(C.ConfigInvalid):
            C.decision({"decision": {"intake_daily_max": 1}})

    def test_manifest_matches_tree(self):
        self.assertTrue(F.manifest_ok())

    def test_kill_is_first(self):
        open(os.path.join(self.etc, "KILL"), "w").close()
        code = self._run({"schema": 1, "call": "route"})
        self.assertEqual(code, 3)

    def test_secret_argv_refuses(self):
        open(os.path.join(self.proc, "1", "cmdline"), "wb").write(b"worker\x00--api-key\x00example")
        code = self._run({"schema": 1, "call": "doctor"})
        self.assertEqual(code, 9)

    def test_dispatch_detaches_and_one_terminal_after_client_dies(self):
        os.environ["DR_FRONT_FAKE_DELAY"] = "0.4"
        body = self._body(PACKET, JOB)
        env = os.environ.copy()
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "dr_front.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, text=True)
        out, _err = None, None
        try:
            proc.stdin.write(json.dumps(body))
            proc.stdin.close()
            line = proc.stdout.readline()
            got = json.loads(line)
            self.assertEqual(got["state"], "accepted", line)
            os.kill(proc.pid, signal.SIGKILL)
        finally:
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill()
        self.assertTrue(self._wait_terminal(JOB), "worker did not finish after the client died")
        self.assertEqual(self._terminal_count(JOB), 1)
        sub = F.handle({"schema": 1, "call": "subscribe", "job_id": JOB, "after_sequence": 0,
                        "until": "terminal", "max_wait_s": 1}, proc_root=self.proc)
        self.assertEqual(sub, 0)
        self.assertEqual(self._terminal_count(JOB), 1)
        again = self._run(body)
        self.assertEqual(again, 0)
        self.assertEqual(open(self.spawn_log).read().count("1"), 1)

    def test_go_gate_blocks_when_required(self):
        import base64
        digest = _sha(PACKET)
        body = {"schema": 1, "call": "dispatch", "job_id": JOB, "intent": "start", "turn": 1,
                "packet_b64": base64.standard_b64encode(PACKET.encode()).decode(),
                "packet_sha256": digest}
        self.assertEqual(self._run(body), 2)
        self.assertFalse(os.path.exists(self.spawn_log))

    def test_unverified_blocks_dispatch_not_route(self):
        cfg = json.load(open(self.cfg))
        cfg["decision"]["backends_verified"] = False
        json.dump(cfg, open(self.cfg, "w"))
        body = self._body(PACKET, JOB)
        body["call"] = "route"
        self.assertEqual(self._run(body), 0)
        body["call"] = "dispatch"
        self.assertEqual(self._run(body), 2)

    def test_second_turn_carries_prior_result(self):
        os.environ["DR_FRONT_FAKE_DELAY"] = "0"
        first = self._body(PACKET, JOB)
        self.assertEqual(self._run(first), 0)
        self.assertTrue(self._wait_terminal(JOB))
        con = sqlite3.connect(os.path.join(self.state, "router.db"))
        sid = con.execute("SELECT session_id FROM jobs WHERE job_id=?", (JOB,)).fetchone()[0]
        con.close()
        second = self._body("Objective: fix the script again.\n", JOB2, session_id=sid, turn=2)
        self.assertEqual(self._run(second), 0)
        self.assertTrue(self._wait_terminal(JOB2))
        con = sqlite3.connect(os.path.join(self.state, "router.db"))
        row = con.execute("SELECT prompt_path FROM jobs WHERE job_id=?", (JOB2,)).fetchone()
        text = open(row[0], encoding="utf-8").read()
        con.close()
        self.assertTrue(text.startswith("Prior result: "), text[:40])

    def test_sha_mismatch_does_not_consume(self):
        import base64
        body = self._body(PACKET, JOB)
        body["packet_sha256"] = "ab" * 32
        self.assertEqual(self._run(body), 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "router.db")) and
                         sqlite3.connect(os.path.join(self.state, "router.db")).execute(
                             "SELECT COUNT(*) FROM jobs").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
