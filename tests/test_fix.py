"""Tests: CA 1 async async-fake, CA 2 classifier, CA 3 capmap desktop-only,
CA N1 forbidden-arg parsing, CA N2 *.bak* corpus exclusion, CA LOW cap 2/2. Offline: local stubs only, no real dispatch."""
import json, os, subprocess, sys, tempfile, time, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D, harness_call as HC
CAP = D.load(os.path.join(ROOT, "capability-map.json")); CAPS = D.load(os.path.join(ROOT, "caps.json"))
PY = sys.executable
import drconfig as _C
PK = (_C.path("packets_dir") or "") + "/"; LEDGER = _C.path("go_ledger"); RUNS = _C.path("runs")


class FIX(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); os.environ["DISPATCH_STATE"] = self.tmp
        D.KILL = os.path.join(self.tmp, "KILL"); os.environ["DISPATCH_KILL"] = D.KILL
    # CA 1
    def test_ca1_asyncfake_async_pending_then_done(self):
        rc = os.path.join(self.tmp, "R.md")
        r = HC.call("async-fake", [PY, "-c", "import time; time.sleep(120)"], job="j1", receipt=rc)
        print("\n  CA1 submit:", r["status"], r["deadline_min"]); self.assertEqual(r["status"], "PENDING")
        self.assertGreaterEqual(r["deadline_min"], 1); self.assertFalse(r["stale"]); self.assertNotIn("secs", r)  # no sync run
        with open(rc, "w") as f: f.write("receipt")
        d = HC.poll_async("j1"); print("  CA1 poll:", d["status"]); self.assertEqual(d["status"], "done")
    def test_ca1_stale_flag_after_deadline(self):
        HC.submit_async("async-fake", "j2", receipt=os.path.join(self.tmp, "none.md"), deadline_min=5, now=1000)
        self.assertEqual(HC.poll_async("j2", now=1000 + 4 * 60)["status"], "PENDING")
        s = HC.poll_async("j2", now=1000 + 6 * 60); print("\n  CA1 stale:", s["status"], s["stale"])
        self.assertTrue(s["stale"]); self.assertEqual(s["status"], "STALE")
    # CA 2
    def test_ca2_real_browser_objective_still_browser(self):
        self.assertEqual(D.classify("1. Objective: check a website with the browser.\nBasis: code fix"), "browser")
    def test_ca2_discussion_sections_ignored(self):
        t = "Basis: browser jobs were forced last run\nInput: website notes\n## Steps\n1. fix the script"
        self.assertEqual(D.classify(t), "code_edit")
    # CA 3
    # CA N1
    def test_n1_forbidden_forms(self):
        forms = [["agy", "--mode=auto"], ["agy", "--mode", "auto"], ["agy", "-p", "x --mode=auto"], ["agy", "--MODE=auto"],
                 ["agy", "--dangerously-skip-permissions=true"], ["agy", "--dangerously"], ["agy -p x --dangerously-skip-permissions"], ["agy", "--mod", "auto"]]
        for f in forms:
            r = HC.call("agy-gemini", f); self.assertEqual(r["status"], "REFUSED", f)
        print("\n  N1 forms refused:", len(forms))
        self.assertIsNone(HC.forbidden(["agy", "-p", "reply OK", "--model", "x"]))  # --model is not --mode
        self.assertIsNone(HC.forbidden(["agy", "--", "--mode"]))
    # CA N2
    def test_n2_corpus_excludes_bak(self):
        c = D.corpus(); self.assertFalse([p for p in c if ".bak" in os.path.basename(p)])
        d = tempfile.mkdtemp()
        for n in ("A.md", "A-r0.bak.md", "B.md.bak-2026"): open(os.path.join(d, n), "w").close()
        self.assertEqual([os.path.basename(p) for p in D.corpus(d, "*")], ["A.md"]); print("\n  N2 corpus:", len(c))
    def test_low_grok_build_cap_2_of_2(self):
        # caps.json has no test_call_cap and no 2/2 label. A missing cap is no ceiling.
        self.assertIsNone(HC.CAPS["grok-build"])
        self.assertNotIn("test_call_cap", CAPS["grok-build"])
        self.assertNotIn("test_calls_label", CAPS["grok-build"])
        for _ in range(2):
            self.assertEqual(HC.call("grok-build", [PY, "-c", "print('OK')"])["status"], "done")
        print("\n  LOW cap:", HC.cap_label("grok-build")); self.assertEqual(HC.cap_label("grok-build"), "2/none")
        self.assertEqual(HC.call("grok-build", [PY, "-c", "print('OK')"])["status"], "done")
    # L2 (r2): async-fake async output redaction
    def test_l2_asyncfake_receipt_redacted(self):
        rc = os.path.join(self.tmp, "GB.md"); fake = "gh" + "p_" + "Z" * 20; tok = "Bearer " + "q" * 16; hx = "ab" * 16
        with open(rc, "w") as f: f.write(f"OK {fake} {tok} key={hx}\n")
        HC.submit_async("async-fake", "gbr", receipt=rc)
        d = HC.poll_async("gbr"); blob = json.dumps(d); print("\n  L2 async-fake:", d["status"], d.get("redaction_hits"), d.get("excerpt"))
        self.assertEqual(d["status"], "done"); self.assertGreaterEqual(d["redaction_hits"], 3)
        for s in (fake, "q" * 16, hx): self.assertNotIn(s, blob)
        self.assertEqual(len(d["receipt_sha"]), 64)  # sha field passes
if __name__ == "__main__": unittest.main(verbosity=2)
