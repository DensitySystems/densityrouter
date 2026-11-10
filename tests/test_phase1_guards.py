"""Phase 1 guards: quota on failures only, packet nofollow, Hosts fail-closed, no lab socket, secret grep."""
import hashlib, os, shutil, sys, tempfile, unittest
sys.dont_write_bytecode = True
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DENSITYROUTER_CONFIG"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "test.config")
sys.path.insert(0, _ROOT)
import dispatcher as D
import harness_call as HC
import live as LV

SECRET_NEEDLES = ("--api-key", "api_key=", "token=")
SCAN_ROOTS = (
    os.path.join(_ROOT, "densityrouter.config.example"),
    os.path.join(_ROOT, "tests", "fixtures"),
)


def _walk_scan():
    for item in SCAN_ROOTS:
        if os.path.isfile(item):
            yield item
            continue
        for dirpath, _dirs, files in os.walk(item):
            for name in files:
                if name.endswith((".json", ".py", ".md", ".example", ".service", ".yml")):
                    yield os.path.join(dirpath, name)


def secret_grep(paths):
    hits = []
    for path in paths:
        try:
            text = open(path, encoding="utf-8").read()
        except (OSError, UnicodeError):
            continue
        for needle in SECRET_NEEDLES:
            if needle in text:
                hits.append((path, needle))
    return hits


class Phase1Guards(unittest.TestCase):
    def test_exit_map_ignores_quota_words_on_success(self):
        self.assertEqual(HC.exit_map(0, False, "the credits field and a 429 in the comment"), "done")
        self.assertEqual(HC.exit_map(1, False, "Error 429 RESOURCE_EXHAUSTED"), "failed_hold:usage")
        self.assertEqual(HC.exit_map(None, True, "credits"), "timed_out")

    def test_timeout_kills_process_group(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        prev = os.environ.get("DISPATCH_STATE")
        self.addCleanup(lambda: os.environ.pop("DISPATCH_STATE", None) if prev is None else os.environ.__setitem__("DISPATCH_STATE", prev))
        pidfile = os.path.join(tmp, "childpid")
        script = os.path.join(tmp, "group.sh")
        open(script, "w").write("#!/bin/sh\nsleep 30 &\necho $! > \"$1\"\nwait\n")
        os.chmod(script, 0o755)
        os.environ["DISPATCH_STATE"] = tmp
        got = HC.call("agy-gemini", argv=[script, pidfile], timeout=1)
        self.assertEqual(got["status"], "timed_out")
        pid = int(open(pidfile).read().strip())
        alive = True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        if alive:
            os.kill(pid, 9)
        self.assertFalse(alive)

    def test_symlink_packet_is_refused_when_live(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        prev = os.environ.get("DISPATCH_STATE")
        self.addCleanup(lambda: os.environ.pop("DISPATCH_STATE", None) if prev is None else os.environ.__setitem__("DISPATCH_STATE", prev))
        real = os.path.join(tmp, "real.md")
        open(real, "w").write("Objective: fix the script.\n")
        link = os.path.join(tmp, "PACKET-link.md")
        os.symlink(real, link)
        sha = D.sha256f(real)
        ledger = os.path.join(tmp, "GO.md")
        runs = os.path.join(tmp, "RUNS.tsv")
        open(ledger, "w").write("| 1 | GO | PACKET-link.md %s |\n" % sha[:12])
        open(runs, "w").write("x\t%s\n" % link)
        state = os.path.join(tmp, "state")
        os.makedirs(state)
        open(os.path.join(state, "LIVE"), "w").close()
        os.environ["DISPATCH_STATE"] = state
        cap = D.load(os.path.join(_ROOT, "capability-map.json"))
        caps = D.load(os.path.join(_ROOT, "caps.json"))
        rc, out = LV.run(link, ledger, runs, cap, caps, env={"DISPATCH_STATE": state},
                         call_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
        self.assertEqual(rc, 8, out["handoff"]["status"])

    def test_hosts_line_fails_closed_without_schema(self):
        saved = dict(D._LAN_ROLE_CACHE)
        env = os.environ.get("DISPATCH_IP_SCHEMA")
        missing = os.path.join(tempfile.mkdtemp(), "no-schema.md")
        os.environ["DISPATCH_IP_SCHEMA"] = missing
        D._LAN_ROLE_CACHE["key"] = None
        try:
            self.assertEqual(D.lan_roles(), set())
            self.assertTrue(D.is_lan("Hosts: app-host\nObjective: fix the script.\n"))
            self.assertFalse(D.is_lan("Hosts: router\nObjective: fix the script.\n"))
        finally:
            D._LAN_ROLE_CACHE.clear()
            D._LAN_ROLE_CACHE.update(saved)
            if env is None:
                os.environ.pop("DISPATCH_IP_SCHEMA", None)
            else:
                os.environ["DISPATCH_IP_SCHEMA"] = env

    def test_route_and_live_do_not_open_sockets(self):
        import inspect
        import dispatcher, live, dr_front
        for mod in (dispatcher, live, dr_front):
            src = inspect.getsource(mod)
            self.assertNotIn("socket.socket", src)
            self.assertNotIn("psql", src)
            self.assertNotIn("192.168.", src)
            self.assertNotIn("10.0.0.", src)

    def test_shipped_templates_have_no_secret_needles(self):
        self.assertEqual(secret_grep(_walk_scan()), [])

    def test_secret_grep_catches_a_synthetic_needle(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "unit.service")
        open(path, "w").write("[Service]\nExecStart=/bin/true --api-key example\n")
        self.assertTrue(secret_grep([path]))

    def test_exact_secret_hash_refuses_the_body(self):
        secret = "synthetic-secret-value"
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "hashes")
        open(path, "w").write(hashlib.sha256(secret.encode()).hexdigest() + "\n")
        self.assertTrue(D.exact_secret_hit("see %s here" % secret, path))
        self.assertFalse(D.exact_secret_hit("no match", path))
        prev = os.environ.get("DISPATCH_STATE")
        os.environ["DISPATCH_STATE"] = tmp
        os.environ["DENSITYROUTER_SECRET_HASHES"] = path
        try:
            got = HC.call("agy-gemini", argv=["/bin/echo", secret])
        finally:
            os.environ.pop("DENSITYROUTER_SECRET_HASHES", None)
            if prev is None:
                os.environ.pop("DISPATCH_STATE", None)
            else:
                os.environ["DISPATCH_STATE"] = prev
        self.assertEqual(got["status"], "secret_refused")
        self.assertNotIn(secret, json_dumps(got))

    def test_redact_failure_returns_class_only(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        prev = os.environ.get("DISPATCH_STATE")
        os.environ["DISPATCH_STATE"] = tmp
        real = D.redact
        def boom(obj, key=None):
            raise RuntimeError("do-not-leak")
        D.redact = boom
        try:
            got = HC.call("agy-gemini", argv=["/bin/echo", "hello"])
        finally:
            D.redact = real
            if prev is None:
                os.environ.pop("DISPATCH_STATE", None)
            else:
                os.environ["DISPATCH_STATE"] = prev
        self.assertEqual(got["status"], "internal_error")
        self.assertEqual(got["error_class"], "RuntimeError")
        self.assertNotIn("do-not-leak", json_dumps(got))
        self.assertEqual(got["stdout"], "")


def json_dumps(obj):
    import json
    return json.dumps(obj)


class GrokTimeout(unittest.TestCase):
    def test_timeout_uses_config_and_fences(self):
        import usage_layer as U
        self.assertEqual(U.session_timeout("grok-build"), 60)
        self.assertEqual(U.session_timeout("cursor"), 180)

        class S:
            restarts = 0
            def ensure(self):
                return self
            def ask(self, prompt, timeout):
                self.timeout = timeout
                return "timeout", "screen"
            def fence(self):
                self.fenced = True
            def start(self):
                raise AssertionError("restart")
        s = S()
        rc, _text, _flag = U.grok_session_call(s, "hi", timeout=None)
        self.assertEqual(rc, "timeout")
        self.assertEqual(s.timeout, 60)
        self.assertTrue(s.fenced)


if __name__ == "__main__":
    unittest.main()
