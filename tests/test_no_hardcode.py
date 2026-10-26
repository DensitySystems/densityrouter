"""Central-config guard: no hardcoded real location or host outside the config files.
Scans the shipped release files only (code, tests, json, docs, the .example config); runtime data
folders (state, usage, ledgers, packets) and local *.bak-* backups are operator data, not release code.
Fails if any of those files contains a home or server path, an org workspace path,
an IPv4 address other than RFC 5737 documentation ranges, or a desktop host name."""
import os, re, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAD = re.compile(r"/ho" r"me/|/s" r"rv/|/work" r"space/|/Us" r"ers/|/ro" r"ot/|\bDESK" r"TOP-[A-Z0-9]+|"
                 r"\b(?!(?:192\.0\.2|198\.51\.100|203\.0\.113)\.)(?:\d{1,3}[.]){3}\d{1,3}\b")
SHIPPED = (".py", ".json", ".md", ".example", ".config", ".tsv", ".txt", ".gitignore")
def files():
    for f in sorted(os.listdir(ROOT)):  # top level: code, json, docs (real densityrouter.config is the one place for real values)
        p = os.path.join(ROOT, f)
        if os.path.isfile(p) and f.endswith(SHIPPED) and f != "densityrouter.config": yield p
    for d, ds, fs in os.walk(os.path.join(ROOT, "tests")):
        ds[:] = [x for x in ds if x != "__pycache__"]
        for f in fs:
            if f.endswith(SHIPPED) and f != "test_no_hardcode.py": yield os.path.join(d, f)
# Internal references that must never ship: gate-log refs, turn ids, real-shaped UUIDs (only all-zero example ids
# are allowed), the removed async bot pool, removed fixtures.
INTERNAL = re.compile(r"GATE" r"-LOG|\bt1\d{3}" r"u\b|\bgrok[-_]b" r"ot\b|ROS" r"TER-INDEX|DISPATCH_ROS" r"TER|"
                      r"\b(?!00000000-0000-)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}" r"-[0-9a-f]{12}\b", re.I)
class NoHardcode(unittest.TestCase):
    def test_no_internal_refs(self):
        hits = []
        for p in files():
            try:
                with open(p, encoding="utf-8") as fh: text = fh.read()
            except (UnicodeError, OSError): continue
            for i, line in enumerate(text.splitlines(), 1):
                if INTERNAL.search(line): hits.append(f"{os.path.relpath(p, ROOT)}:{i}")
        self.assertEqual(hits, [], "internal reference in shipped files:\n" + "\n".join(hits))
    def test_internal_guard_catches(self):
        for s in ("GATE" "-LOG", "t1234" "u", "grok" + "-bot", "12345678-9abc-4def-8123-" "456789abcdef"): self.assertTrue(INTERNAL.search(s), s)
        self.assertFalse(INTERNAL.search("00000000-0000-7000-8000-000000000001"))
    def test_no_hardcoded_paths_or_hosts(self):
        hits = []
        for p in files():
            try:
                with open(p, encoding="utf-8") as fh: text = fh.read()
            except (UnicodeError, OSError): continue
            for i, line in enumerate(text.splitlines(), 1):
                if BAD.search(line): hits.append(f"{os.path.relpath(p, ROOT)}:{i}: {line.strip()[:120]}")
        self.assertEqual(hits, [], "hardcoded path/host outside the config:\n" + "\n".join(hits))
    def test_guard_catches(self):
        for s in ("/ho" "me/u/x", "/s" "rv/a", "10.1." "2.3", "DESK" "TOP-AB12"): self.assertTrue(BAD.search(s), s)
        for s in ("203.0.113.7", "~/.local/bin/agy", "lab.invalid"): self.assertFalse(BAD.search(s), s)
if __name__ == "__main__": unittest.main()
