"""Phase 1C: --harness pin and async-fake queued packet record. stdlib unittest.
Temp DISPATCH_STATE only. No real state/ dir, no caps.json writes.
Run: python3 tests/test_harness_pin.py
"""
import hashlib, json, os, shutil, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, harness_call as HC, live as LV

RESEARCH = (
    "Objective: Research the queue notes.\n"
    "Placement: local\n"
)
LAN = (
    "Objective: Research the queue notes.\n"
    "Placement: LAN\n"
)
OLD_RECORD_KEYS = {"harness", "job", "status", "receipt", "submitted", "deadline_min", "deadline", "stale"}

class HarnessPin(unittest.TestCase):
    def setUp(self):
        self._state_snap = self._state_files()
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_KILL")}
        os.environ["DISPATCH_STATE"] = self.tmp
        self._kill = D.KILL
        D.KILL = os.path.join(self.tmp, "KILL"); os.environ["DISPATCH_KILL"] = D.KILL
        self.capmap = D.load(os.path.join(ROOT, "capability-map.json"))
        self.caps = json.loads(json.dumps(D.load(os.path.join(ROOT, "caps.json"))))
        self.calls = []
    def tearDown(self):
        os.environ.pop("DISPATCH_Q_SENTINEL", None)
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
    def _call(self, h, job=None, packet=None, **k):
        self.calls.append((h, packet))
        return {"harness": h, "status": "done"}
    def _caps(self):
        return json.loads(json.dumps(self.caps))
    def _go(self, text, name, caps=None, omit_pin=False, pin="cursor"):
        self.calls = []
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f: f.write(text)
        sha12 = D.sha256f(path)[:12]
        ledger = os.path.join(self.tmp, name + ".ledger")
        runs = os.path.join(self.tmp, name + ".runs")
        with open(ledger, "w", encoding="utf-8") as f: f.write("| 1 | GO | %s %s |\n" % (name, sha12))
        with open(runs, "w", encoding="utf-8") as f: f.write("x\t%s\n" % path)
        kw = {"env": {"DISPATCH_LIVE": "1", "DISPATCH_STATE": self.tmp},
              "call_fn": self._call}
        if not omit_pin: kw["pin"] = pin
        rc, out = LV.run(path, ledger, runs, self.capmap, self.caps if caps is None else caps, **kw)
        return rc, out, path

    def test_pin_honored(self):
        # Research ranks agy-gemini first; cursor is ranked and is not the balancer choice.
        text = (
            "Objective: Research the queue notes.\n"
            "Placement: local\n"
        )
        rc, out, path = self._go(text, "PACKET-PIN-RESEARCH.md", pin="cursor")
        row = next(r for r in out["route"]["ranked"] if r["harness"] == "cursor")
        self.assertNotEqual(out["route"]["ranked"][0]["harness"], "cursor")
        self.assertNotIn("async-fake", [r["harness"] for r in out["route"]["ranked"]])
        self.assertIsNotNone(row["score"]); self.assertGreater(row["H"], 0)
        self.assertFalse(out["route"]["lan"])
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(self.calls, [("cursor", path)])
        self.assertEqual(out["handoff"]["would_send_to"], "cursor")
        self.assertTrue(out["handoff"]["pinned"])
        self.assertEqual(out["handoff"]["balancer_choice"], out["route"]["ranked"][0]["harness"])
        # async-fake is excluded even when the packet names some other owner.
        rc_x, out_x, _ = self._go(text, "PACKET-PIN-EXCLUDED.md", pin="async-fake")
        self.assertEqual(rc_x, 7, out_x["handoff"]["status"])
        self.assertEqual(self.calls, [])
        self.assertEqual(out_x["handoff"]["status"],
            "HOLD (live: pinned harness async-fake refused: excluded from routing (config route=false))")
        self.assertNotIn("call", out_x)
        self.assertNotIn("mint", out_x)
        self.assertIsNone(D.lease_held(out_x["handoff"]["job"]))

    def test_pin_enabled_false_is_not_a_refusal(self):
        caps = self._caps(); caps["cursor"]["enabled"] = False
        text = (
            "Objective: Research the queue notes.\n"
            "Placement: local\n"
        )
        rc, out, path = self._go(text, "PACKET-PIN-RESEARCH.md", caps=caps, pin="cursor")
        self.assertNotIn("cursor", [h for h, _why in out["route"]["skipped"]])
        self.assertIn("cursor", [row["harness"] for row in out["route"]["ranked"]])
        self.assertNotIn("async-fake", [row["harness"] for row in out["route"]["ranked"]])
        self.assertNotIn("async-fake", [h for h, _why in out["route"]["skipped"]])
        self.assertFalse(any("disabled" in why for _h, why in out["route"]["skipped"]))
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(self.calls, [("cursor", path)])
        self.assertNotIn("hard: disabled", out["handoff"]["status"])

    def test_pin_refused_over_cap(self):
        # route() has no cap gate. used_today >= daily_cap does not refuse a pin.
        caps = self._caps(); caps["cursor"]["enabled"] = True
        caps["cursor"]["daily_cap"] = 2; caps["cursor"]["used_today"] = 2
        text = (
            "Objective: Research the queue notes.\n"
            "Placement: local\n"
        )
        rc, out, path = self._go(text, "PACKET-PIN-RESEARCH.md", caps=caps, pin="cursor")
        self.assertNotIn("cursor", [h for h, _why in out["route"]["skipped"]])
        self.assertNotIn("async-fake", [row["harness"] for row in out["route"]["ranked"]])
        self.assertFalse(any("cap" in why for _h, why in out["route"]["skipped"]))
        self.assertEqual(rc, 0, out["handoff"]["status"])
        self.assertEqual(self.calls, [("cursor", path)])
        self.assertNotIn("daily cap", out["handoff"]["status"])

    def test_pin_refused_lan(self):
        rc, out, _ = self._go(LAN, "PACKET-PIN-LAN.md", pin="cursor")
        self.assertTrue(out["route"]["lan"])
        self.assertEqual(rc, 7)
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "leases")))
        self.assertEqual(out["handoff"]["status"],
            "HOLD (live: pinned harness cursor refused: hard: LAN work -> grok-build only (config lan.harness))")
        self.assertNotIn("async-fake", [row["harness"] for row in out["route"]["ranked"]])
        self.assertNotIn("async-fake", [h for h, _why in out["route"]["skipped"]])

    def test_pin_refused_unknown_name(self):
        rc, out, _ = self._go(RESEARCH, "PACKET-PIN-RESEARCH.md", pin="not-a-harness")
        self.assertNotIn("not-a-harness", D.FALLBACK)
        self.assertEqual(rc, 7)
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "leases")))
        self.assertEqual(out["handoff"]["status"],
            "HOLD (live: pinned harness not-a-harness refused: not ranked)")

    def test_no_pin_unchanged(self):
        rc1, out1, _ = self._go(RESEARCH, "PACKET-PIN-RESEARCH.md", pin=None)
        rc2, out2, _ = self._go(RESEARCH, "PACKET-PIN-RESEARCH.md", omit_pin=True)
        self.assertEqual(rc1, rc2)
        def _strip(o):  # metering timestamps differ per run
            o = json.loads(json.dumps(o))
            for u in o.get("usage", []): u.pop("t_start", None); u.pop("t_end", None)
            return o
        self.assertEqual(json.dumps(_strip(out1), sort_keys=True), json.dumps(_strip(out2), sort_keys=True))
        self.assertEqual(out1["handoff"]["would_send_to"], out1["route"]["ranked"][0]["harness"])
        self.assertEqual(out2["handoff"]["would_send_to"], out2["route"]["ranked"][0]["harness"])
        self.assertNotIn("pinned", out1["handoff"])
        self.assertNotIn("balancer_choice", out1["handoff"])

    def test_queued_record_holds_path_sha_text(self):
        text = "tail  \u2603  "
        self.assertTrue(text.endswith(" "))
        self.assertNotIn("\n", text)
        raw = text.encode("utf-8")
        path = os.path.join(self.tmp, "queued.txt")
        with open(path, "wb") as f: f.write(raw)
        sentinel = "PHASE1C-SENTINEL-not-a-file-byte"
        os.environ["DISPATCH_Q_SENTINEL"] = sentinel
        rec = HC.submit_async("async-fake", "job-packet", packet=path, now=1000)
        self.assertEqual(rec["packet"], path)
        self.assertEqual(rec["packet_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(rec["packet_text"], text)
        self.assertNotIn(sentinel, json.dumps(rec))
        with open(HC.pending_path("job-packet")) as f: saved = json.load(f)
        self.assertEqual(saved["packet_text"], text)
        self.assertEqual(saved["packet_sha256"], rec["packet_sha256"])

    def test_queued_record_without_packet_unchanged(self):
        rec = HC.submit_async("async-fake", "job-bare", now=1000)
        self.assertEqual(set(rec), OLD_RECORD_KEYS)

    def test_caps_json_unchanged_phase_1c(self):
        with open(os.path.join(ROOT, "caps.json"), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.assertTrue(digest.startswith("02a81b2eb0a8"))

if __name__ == "__main__": unittest.main(verbosity=2)
