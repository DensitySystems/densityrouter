"""Phase 1D: a pin is honoured after all checks; a pin to a non-route harness is refused; redaction hits are refused.
Temp DISPATCH_STATE only. stdlib unittest.
Run: python3 tests/test_harness_1d.py
"""
import hashlib, json, os, shutil, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, harness_call as HC, live as LV

SECRET = "sk" + "-testkey1234567890"

def research(owner_line, extra=""):
    lines = []
    if owner_line:
        lines.append(owner_line)
    lines.append("Objective: Research the queue notes.")
    lines.append("Placement: local")
    if extra:
        lines.append(extra)
    return "\n".join(lines) + "\n"

class Harness1D(unittest.TestCase):
    def setUp(self):
        self._state_snap = self._state_files()
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL", "DISPATCH_LIVE")}
        os.environ["DISPATCH_STATE"] = self.tmp
        os.environ.pop("DISPATCH_LIVE", None)
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL"); os.environ["DISPATCH_KILL"] = D.KILL
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = D.load(os.path.join(ROOT, "caps.json"))
        self.calls = []
    def tearDown(self):
        D.KILL = self._kill
        for k, v in self._env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(self._state_files(), self._state_snap)
    def _state_files(self):
        base = os.path.join(ROOT, "state")
        if not os.path.isdir(base): return []
        found = []
        for dirpath, _dirnames, filenames in os.walk(base):
            found.extend(os.path.join(dirpath, name) for name in filenames)
        return sorted(found)
    def _call(self, h, job=None, packet=None, **k):
        self.calls.append((h, packet))
        return {"harness": h, "status": "done"}
    def _env_live(self, live):
        env = {"DISPATCH_STATE": self.tmp}
        if live: env["DISPATCH_LIVE"] = "1"
        return env
    def _go(self, text, name, pin="grok-build", live=True, omit_pin=False):
        self.calls = []
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f: f.write(text)
        sha12 = D.sha256f(path)[:12]
        ledger = os.path.join(self.tmp, name + ".ledger")
        runs = os.path.join(self.tmp, name + ".runs")
        with open(ledger, "w", encoding="utf-8") as f: f.write("| 1 | GO | %s %s |\n" % (name, sha12))
        with open(runs, "w", encoding="utf-8") as f: f.write("x\t%s\n" % path)
        kw = {"env": self._env_live(live),
              "call_fn": self._call}
        if not omit_pin: kw["pin"] = pin
        rc, out = LV.run(path, ledger, runs, self.capmap, self.caps, **kw)
        return rc, out, path
    def _assert_nothing_sent(self):
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "leases")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pending")))

    def test_pin_passes(self):
        text = research(None)
        rc, out, path = self._go(text, "PACKET-1D-MATCH.md", pin="grok-build", live=False)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(out["mode"], "offline")
        self.assertEqual(out["handoff"]["would_send_to"], "grok-build")
        self.assertTrue(out["handoff"]["pinned"])
        self._assert_nothing_sent()
        rc, out, path = self._go(text, "PACKET-1D-MATCH.md", pin="grok-build", live=True)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(out["mode"], "live")
        self.assertEqual(self.calls, [("grok-build", path)])
        self.assertEqual(out["handoff"]["would_send_to"], "grok-build")
        self.assertTrue(out["handoff"]["pinned"])
        self.assertIn("ROUTED LIVE -> grok-build", out["handoff"]["status"])
        self.assertIsNone(D.lease_held(out["handoff"]["job"]))
        # A pin to a non-routed harness is excluded from routing.
        text_bot = research(None)
        rc, out, _path = self._go(text_bot, "PACKET-1D-MATCH-BOT.md", pin="async-fake", live=True)
        self.assertEqual(rc, 7, out["handoff"]["status"])
        self.assertEqual(out["handoff"]["status"],
            "HOLD (live: pinned harness async-fake refused: excluded from routing (config route=false))")
        self.assertNotIn("async-fake", [row["harness"] for row in out["route"]["ranked"]])
        self.assertEqual(self.calls, [])


    def test_pin_none_leaves_balancer_choice(self):
        text = research(None)
        rc, out, path = self._go(text, "PACKET-1D-NOPIN.md", live=True, omit_pin=True)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertNotIn("pinned", out["handoff"])
        self.assertEqual(out["handoff"]["would_send_to"], out["route"]["ranked"][0]["harness"])
        self.assertEqual(self.calls, [(out["route"]["ranked"][0]["harness"], path)])

    def test_live_redaction_hits_rc8_nothing_called(self):
        text = research(None, "Token: %s" % SECRET)
        n = D.redact_text(text)[1]
        self.assertGreater(n, 0)
        rc, out, _ = self._go(text, "PACKET-1D-DIRTY.md", live=True, omit_pin=True)
        self.assertEqual(rc, 8, out["handoff"]["status"])
        self.assertEqual(out["packet_redaction_hits"], n)
        self.assertEqual(out["handoff"]["status"],
            "HOLD (live: packet has %d redaction hits, refused, nothing queued)" % n)
        self.assertNotIn("call", out)
        self.assertNotIn("mint", out)
        self.assertNotIn(SECRET, json.dumps(out))
        self._assert_nothing_sent()
        rc, out, path = self._go(text, "PACKET-1D-DIRTY-PIN.md", pin="grok-build", live=True)
        self.assertEqual(rc, 8, out["handoff"]["status"])
        self.assertEqual(out["packet_redaction_hits"], n)
        self.assertEqual(self.calls, [])
        self._assert_nothing_sent()

    def test_offline_redaction_does_not_exit_8(self):
        text = research(None, "Token: %s" % SECRET)
        rc, out, _ = self._go(text, "PACKET-1D-DIRTY-OFF.md", live=False, omit_pin=True)
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(out["mode"], "offline")
        self.assertNotIn("packet_redaction_hits", out)
        self._assert_nothing_sent()

    def test_submit_async_redaction_refused_no_pending(self):
        text = research(None, "Token: %s" % SECRET)
        n = D.redact_text(text)[1]
        self.assertGreater(n, 0)
        path = os.path.join(self.tmp, "dirty.txt")
        with open(path, "w", encoding="utf-8") as f: f.write(text)
        rec = HC.submit_async("async-fake", "job-dirty", packet=path, now=1000)
        self.assertEqual(rec, {"harness": "async-fake", "job": "job-dirty", "status": "REFUSED",
                               "reason": "packet has redaction hits", "redaction_hits": n})
        self.assertNotIn(SECRET, json.dumps(rec))
        self.assertFalse(os.path.exists(HC.pending_path("job-dirty")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pending")))
        called = HC.call("async-fake", job="job-call", packet=path)
        self.assertEqual(called["status"], "REFUSED")
        self.assertEqual(called["reason"], "packet has redaction hits")
        self.assertEqual(called["redaction_hits"], n)
        self.assertNotIn(called["status"], ("done", "PENDING"))
        for key in ("packet", "packet_sha256", "packet_text"):
            self.assertNotIn(key, called)
        self.assertNotIn(SECRET, json.dumps(called))
        self.assertFalse(os.path.exists(HC.pending_path("job-call")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pending")))
        self.assertEqual(HC.used("async-fake"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.json")))

    def test_submit_async_clean_stores_path_sha_text(self):
        text = "Objective: Research the queue notes.\nTail \u2603  "
        self.assertTrue(text.endswith(" "))
        self.assertEqual(D.redact_text(text)[1], 0)
        raw = text.encode("utf-8")
        path = os.path.join(self.tmp, "clean.txt")
        with open(path, "wb") as f: f.write(raw)
        rec = HC.submit_async("async-fake", "job-clean", packet=path, now=1000)
        self.assertEqual(rec["status"], "PENDING")
        self.assertEqual(rec["packet"], path)
        self.assertEqual(rec["packet_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(rec["packet_text"], text)
        with open(HC.pending_path("job-clean")) as f: saved = json.load(f)
        self.assertEqual(saved["packet"], path)
        self.assertEqual(saved["packet_sha256"], rec["packet_sha256"])
        self.assertEqual(saved["packet_text"], text)

if __name__ == "__main__": unittest.main(verbosity=2)
