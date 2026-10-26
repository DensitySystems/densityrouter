"""Classifier negation fix (2026-10-25): a negated clause is dropped, not the whole line.
Includes the wording that misfired (read-only summary packet routed as code_edit)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DENSITYROUTER_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "test.config"))
import dispatcher as D

MISFIRE = ("Read ~/scratch/router-build/e2e/dispatcher/README.md and reply with a 3-line summary.\n"
           "Read-only: do not edit, create or delete any file.\n")

class Negation(unittest.TestCase):
    def cls(self, t): return D.classify(t)[0] if isinstance(D.classify(t), tuple) else D.classify(t)
    def test_misfire_wording_is_research(self):
        self.assertEqual(self.cls(MISFIRE), "research")
    def test_misfire_wording_as_objective_is_research(self):
        self.assertEqual(self.cls("Objective: Read the README and reply with a 3-line summary. Read-only: do not edit, create or delete any file.\n"), "research")
    def test_readonly_line_keeps_signal(self):
        self.assertIn("Read-only", D.classify_text("Read-only: do not edit, create or delete any file."))
        self.assertNotIn("edit", D.classify_text("Read-only: do not edit, create or delete any file."))
    def test_dont_modify_files_has_no_code_signal(self):
        self.assertNotIn("modify", D.classify_text("Don't modify files"))
    def test_do_not_fix_build_script_is_not_code_edit_signal(self):
        t = D.classify_text("Summarize the CI logs; do not fix the build script.")
        self.assertNotRegex(t, r"(?i)fix|build|script")
        self.assertEqual(self.cls("Summarize the CI logs; do not fix the build script."), "research")
    def test_positive_code_clause_still_code_edit(self):
        self.assertEqual(self.cls("Fix the build script. Do not touch the docs."), "code_edit")
    def test_whole_line_markers_still_drop(self):
        self.assertEqual(D.classify_text("Browser work stays off for this job."), "")
        self.assertEqual(D.classify_text("refactor is barred"), "")
if __name__ == "__main__": unittest.main()
