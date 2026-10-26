"""Phase 1A: routed invoke argv carries the packet file text. No real grok/agy, no network.
subprocess is stubbed. Temp DISPATCH_STATE only. stdlib unittest.
Run: python3 tests/test_harness_packet.py
"""
import os, shutil, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, harness_call as HC, live as LV

TEXT = (
    "PACKET-PHASE1A\n"
    "say \"keep exact\" {prompt}\n"
    "reply OK is the packet body, not a template\n"
)

class HarnessPacket(unittest.TestCase):
    def setUp(self):
        self._state_snap = self._state_files()
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL")}
        os.environ["DISPATCH_STATE"] = self.tmp
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL"); os.environ["DISPATCH_KILL"] = D.KILL
        self.packet = os.path.join(self.tmp, "PACKET-PHASE1A.md")
        with open(self.packet, "w") as f: f.write(TEXT)
        self.ran = []
        self._run = HC.subprocess.run
        def fake_run(argv, **k):
            self.ran.append(list(argv))
            class P:
                returncode = 0; stdout = "OK"; stderr = ""
            return P()
        HC.subprocess.run = fake_run
    def tearDown(self):
        HC.subprocess.run = self._run
        D.KILL = self._kill
        for k, v in self._env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(self._state_files(), self._state_snap)  # never wrote the real state dir
    def _state_files(self):
        base = os.path.join(ROOT, "state")
        if not os.path.isdir(base): return []
        found = []
        for dirpath, _dirnames, filenames in os.walk(base):
            found.extend(os.path.join(dirpath, name) for name in filenames)
        return sorted(found)
    def _disk(self):
        with open(self.packet) as f: return f.read()

    def test_routed_argv_is_exact_packet_text(self):
        disk = self._disk()
        self.assertEqual(disk, TEXT)
        expect = {
            "grok-build": [HC.GROK_BUILD_BIN, "-p", disk],
            "agy-gemini": [HC.BINS["agy-gemini"], "-p", disk],
        }
        for h, argv in expect.items():
            self.ran.clear()
            built = HC.build_argv(h, disk)
            self.assertEqual(built, argv)  # pure helper, same text call() will pass
            r = HC.call(h, packet=self.packet)
            self.assertEqual(r["status"], "done", r)
            self.assertEqual(self.ran, [argv])
            self.assertEqual(self.ran[0][2], disk)
            self.assertEqual(HC.used(h), 1)
        self.assertNotEqual(disk, "reply OK")

    def test_every_template_slot_and_no_reply_ok(self):
        with open(os.path.join(ROOT, "harness_call.py")) as f: src = f.read()
        self.assertNotIn("reply OK", src)
        self.assertEqual(HC.INVOKE["cursor"][1:], ["-p", "--trust", "--output-format", "text", "--", HC.PROMPT]); self.assertTrue(os.path.isabs(HC.INVOKE["cursor"][0]))
        self.assertIsNone(HC.INVOKE["async-fake"])
        seen = 0
        for h, tmpl in HC.INVOKE.items():
            built = HC.build_argv(h, TEXT)
            if tmpl is None:
                self.assertIsNone(built, h); continue
            seen += 1
            blob = " ".join(a for a in tmpl if isinstance(a, str))
            self.assertNotIn("reply OK", blob, h)
            self.assertNotIn("reply OK", tmpl, h)
            self.assertEqual(len(built), len(tmpl), h)
            replaced = [i for i, (a, b) in enumerate(zip(tmpl, built)) if a != b]
            self.assertTrue(replaced, h)
            for i in replaced: self.assertEqual(built[i], TEXT)
            for i, (a, b) in enumerate(zip(tmpl, built)):
                if i not in replaced: self.assertEqual(a, b)
        self.assertGreaterEqual(seen, 2)  # grok-build and agy-gemini, and any later template
        self.assertEqual(self.ran, [])  # build_argv does not invoke
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.json")))

    def test_no_packet_does_not_smoke_ping(self):
        for h in ("grok-build", "agy-gemini"):
            for kwargs in ({}, {"argv": None}, {"packet": None}, {"argv": None, "packet": None}):
                r = HC.call(h, **kwargs)
                self.assertNotEqual(r["status"], "done", (h, kwargs, r))
                self.assertEqual(r["status"], "REFUSED", r)
                self.assertEqual(HC.used(h), 0)
        self.assertEqual(self.ran, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.json")))

    def test_forbidden_flags_still_refused(self):
        forms = [
            ["agy", "--mode=auto"],
            ["agy", "--mode", "auto"],
            ["agy", "-p", "x --mode=auto"],
            ["agy", "--MODE=auto"],
            ["agy", "--dangerously-skip-permissions=true"],
            ["agy", "--dangerously"],
            ["agy -p x --dangerously-skip-permissions"],
            ["agy", "--mod", "auto"],
            ["grok", "-p", TEXT, "--mode", "auto"],
        ]
        for f in forms:
            r = HC.call("agy-gemini", f, packet=self.packet)  # explicit argv wins; still scanned
            self.assertEqual(r["status"], "REFUSED", f)
            self.assertIn("forbidden", r["reason"])
        bad = os.path.join(self.tmp, "bad.md")
        with open(bad, "w") as f: f.write("do the work --mode=auto\n")
        r = HC.call("agy-gemini", packet=bad)
        self.assertEqual(r["status"], "REFUSED", r)
        r = HC.call("grok-build", packet=bad)
        self.assertEqual(r["status"], "REFUSED", r)
        self.assertEqual(self.ran, [])
        self.assertEqual(HC.used("agy-gemini"), 0)
        self.assertEqual(HC.used("grok-build"), 0)
        self.assertIsNone(HC.forbidden(["agy", "-p", "reply OK", "--model", "x"]))

    def test_explicit_argv_and_async_cursor_unchanged(self):
        argv = [sys.executable, "-c", "print('not-run')"]
        r = HC.call("grok-build", argv)  # existing caller: argv, no packet
        self.assertEqual((r["status"], self.ran), ("done", [argv]))
        self.ran.clear()
        r = HC.call("async-fake", packet=self.packet, job="jb")
        self.assertEqual(r["status"], "PENDING")
        fake = os.path.join(self.tmp, "cursor-agent"); open(fake, "w").close()  # fake CLI; subprocess stubbed
        old0 = HC.INVOKE["cursor"][0]; HC.INVOKE["cursor"][0] = fake
        try: cursor = HC.call("cursor", packet=self.packet); want = HC.build_argv("cursor", TEXT)
        finally: HC.INVOKE["cursor"][0] = old0
        self.assertEqual(cursor["status"], "done")
        self.assertNotEqual(cursor["status"], "BLOCKED")
        self.assertEqual(want, [fake, "-p", "--trust", "--output-format", "text", "--", TEXT])
        self.assertEqual(self.ran, [want])

    def test_live_forwards_packet_path(self):
        pk = os.path.join(self.tmp, "PACKET-PKT-r1.md")
        with open(pk, "w") as f:
            f.write("# PACKET: pkt\n\n## Objective\nFix a script in the scratch dir.\n")
        sha12 = D.sha256f(pk)[:12]
        ledger = os.path.join(self.tmp, "ledger.md"); runs = os.path.join(self.tmp, "RUNS.tsv")
        with open(ledger, "w") as f: f.write("| 1 | GO | PACKET-PKT-r1.md %s |\n" % sha12)
        with open(runs, "w") as f: f.write("x\t%s\n" % pk)
        seen = []
        def fake(h, job=None, packet=None, **k):
            seen.append(packet); return {"harness": h, "status": "done", "stdout": "OK"}
        capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        caps = {"grok-build": {"enabled": True}, "cursor": {"enabled": False}, "agy-gemini": {"enabled": False}, "async-fake": {"enabled": False}}
        rc, out = LV.run(pk, ledger, runs, capmap, caps, env={"DISPATCH_LIVE": "1", "DISPATCH_STATE": self.tmp},
                         call_fn=fake)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [pk])
        self.assertEqual(self.ran, [])

if __name__ == "__main__": unittest.main(verbosity=2)
