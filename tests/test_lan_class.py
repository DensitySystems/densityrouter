"""Phase 1B r2: LAN is declared fields and the Objective line only. Stdlib unittest."""
import os, shutil, sys, tempfile, unittest
sys.dont_write_bytecode = True
import os as _os; _os.environ["DENSITYROUTER_CONFIG"] = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "fixtures", "test.config"); assert _os.environ["DENSITYROUTER_CONFIG"].endswith(_os.path.join("fixtures", "test.config"))
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
import dispatcher as D
CAP = D.load(os.path.join(ROOT, "capability-map.json")); CAPS = D.load(os.path.join(ROOT, "caps.json"))

SCHEMA = """# Draft

Roles are not taken from this table.

| Node | Role | FQDN |
| --- | --- | --- |
| x | no | not-a-role.lab.invalid |

## Static assignments

| Node | Role | FQDN |
| --- | --- | --- |
| n1 | edge | rack-a.lab.invalid |
| n2 | edge | rack-b.lab.invalid |
| n3 | book | vault.lab.invalid |
| n4 | desk | nd-07.lab.invalid |
| n5 | bare | router.lab.invalid |
| n6 | bare | switch.lab.invalid |
| n7 | bare | firewall.lab.invalid |
| n8 | bare | homelab.lab.invalid |
| n9 | skip | \u2014 |
| n10 | skip | - |
| n11 | skip | tbd.lab.invalid |
| n12 | skip | |

## Other

| Node | Role | FQDN |
| --- | --- | --- |
| n13 | no | parser-outside.lab.invalid |
"""

class LanClass(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("DISPATCH_STATE", "DISPATCH_IP_SCHEMA")}
        self.tmp = tempfile.mkdtemp()
        os.environ["DISPATCH_STATE"] = self.tmp
        self.schema = os.path.join(self.tmp, "ip-schema.md")
        with open(self.schema, "w", encoding="utf-8") as f:
            f.write(SCHEMA)
        os.environ["DISPATCH_IP_SCHEMA"] = self.schema

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_placement_router_is_not_lan(self):
        self.assertFalse(D.is_lan("Placement: router\n"))
        self.assertFalse(D.is_lan("Placement: switch harness\n"))
        self.assertFalse(D.is_lan("Hosts: router\n"))
        self.assertNotIn("router", D.lan_roles())
        self.assertNotIn("switch", D.lan_roles())

    def test_placement_firewall_and_homelab_are_not_lan(self):
        self.assertFalse(D.is_lan("Placement: firewall\n"))
        self.assertFalse(D.is_lan("Placement: homelab\n"))
        self.assertFalse(D.is_lan("Hosts: firewall\n"))
        self.assertFalse(D.is_lan("Hosts: homelab\n"))
        self.assertFalse(D.is_lan("Network placement: firewall\n"))
        roles = D.lan_roles()
        self.assertNotIn("firewall", roles)
        self.assertNotIn("homelab", roles)

    def test_placement_explicit_lan_forces_grok_build(self):
        self.assertTrue(D.is_lan("Hosts: none\nPlacement: LAN\n"))
        r = D.route("Placement: LAN\n", CAP, CAPS)
        self.assertTrue(r["lan"])
        self.assertEqual([row["harness"] for row in r["ranked"]], ["grok-build"])
        skipped = dict(r["skipped"])
        self.assertEqual(skipped["agy-gemini"], "hard: LAN work -> grok-build only (config lan.harness)")
        self.assertEqual(skipped["cursor"], "hard: LAN work -> grok-build only (config lan.harness)")
        self.assertNotIn("async-fake", skipped)
        self.assertNotIn("async-fake", [row["harness"] for row in r["ranked"]])
        self.assertNotIn("cursor", [row["harness"] for row in r["ranked"]])

    def test_placement_on_the_lan_and_lan_host_are_lan(self):
        self.assertTrue(D.is_lan("Placement: on the LAN\n"))
        self.assertTrue(D.is_lan("Placement: LAN host\n"))
        self.assertTrue(D.is_lan("Placement: LAN hosts\n"))
        self.assertTrue(D.is_lan("Network placement: LAN\n"))
        self.assertTrue(D.is_lan("Host: lan host\n"))
        self.assertTrue(D.is_lan("- Placement: On the LAN\n"))

    def test_placement_none_na_local_is_not_lan(self):
        for line in ("Placement: none", "Placement: n/a", "Placement: local",
                     "Placement: None. No listener.", "Placement: N/A", "Placement: LOCAL",
                     "Network placement: none", "Hosts: none", "Hosts: n/a", "Hosts: local",
                     "Host: N/A, local only"):
            self.assertFalse(D.is_lan(line + "\nLAN host\nrack-a\n"), line)

    def test_objective_clause_explicit_lan_is_lan(self):
        self.assertTrue(D.is_lan("Objective: build X. Placement: LAN\n"))
        self.assertTrue(D.is_lan("Objective: build X on the LAN\n"))
        self.assertTrue(D.is_lan("1. Objective: build X. Placement: LAN hosts\n"))

    def test_objective_prose_router_is_not_lan(self):
        self.assertFalse(D.is_lan("Objective: configure the router before lunch\n"))
        self.assertFalse(D.is_lan("Objective: fix Rack-A tonight\n"))
        self.assertFalse(D.is_lan("1. Objective: remove duplicates on nd-07 /home\n"))

    def test_body_without_declaration_is_not_lan(self):
        text = "\n".join([
            "The router, switch, firewall and homelab stay as they are.",
            "rack-a rack-b vault nd-07",
            "LAN host",
            "on the LAN",
            "A LAN host is mentioned in passing.",
        ])
        self.assertFalse(D.is_lan(text))

    def test_non_lan_research_still_scores_by_ability(self):
        text = "Objective: research the packet literature.\nPlacement: local\n"
        r = D.route(text, CAP, CAPS)
        self.assertFalse(r["lan"])
        self.assertEqual(r["task_type"], "research")
        self.assertEqual(r["ranked"][0]["harness"], "agy-gemini")
        skipped = dict(r["skipped"])
        self.assertNotIn("cursor", skipped)
        self.assertIn("cursor", [row["harness"] for row in r["ranked"]])

    def test_caps_json_unchanged_phase_1b(self):
        got = D.sha256f(os.path.join(ROOT, "caps.json"))
        self.assertTrue(got.startswith("02a81b2eb0a8"))

    def test_hosts_role_from_schema_is_lan(self):
        self.assertEqual(D.lan_roles(), {"racka", "rackb", "vault", "nd7"})
        self.assertTrue(D.is_lan("Hosts: Rack-A\n"))
        self.assertTrue(D.is_lan("Hosts: nd7\n"))
        self.assertTrue(D.is_lan("Hosts: vault\n"))
        self.assertFalse(D.is_lan("Hosts: someotherbox\n"))
        self.assertTrue(D.is_lan("Hosts: ND-07\n"))
        self.assertTrue(D.is_lan("Hosts: nd07\n"))
        self.assertTrue(D.is_lan("Hosts: nd-07\n"))
        self.assertTrue(D.is_lan("Hosts: racka\n"))
        self.assertTrue(D.is_lan("Host: rack-b\n"))
        self.assertTrue(D.is_lan("Objective: ship it. Hosts: vault\n"))
        self.assertTrue(D.is_lan("Placement: none\nHosts: Rack-A\n"))
        self.assertFalse(D.is_lan("Placement: rack-a\n"))

    def test_schema_missing_fails_closed(self):
        missing = os.path.join(self.tmp, "no-such-schema.md")
        os.environ["DISPATCH_IP_SCHEMA"] = missing
        self.assertEqual(D.lan_roles(), set())
        self.assertTrue(D.is_lan("Hosts: someotherbox\n"))
        self.assertFalse(D.is_lan("Hosts: router\n"))
        self.assertFalse(D.is_lan("Hosts: none\n"))
        self.assertFalse(D.is_lan("Hosts: n/a\n"))
        self.assertFalse(D.is_lan("Hosts: local\n"))
        self.assertFalse(D.is_lan("Hosts: switch\n"))
        self.assertFalse(D.is_lan("Hosts: firewall\n"))
        self.assertFalse(D.is_lan("Hosts: homelab\n"))
        self.assertFalse(D.is_lan("Objective: fix Rack-A\n"))
        self.assertFalse(D.is_lan("The LAN host is mentioned here\n"))
        self.assertTrue(D.is_lan("Placement: LAN\n"))

    def test_roles_read_live_from_schema(self):
        self.assertFalse(D.is_lan("Hosts: parser-9\n"))
        self.assertNotIn("parser9", D.lan_roles())
        with open(self.schema, encoding="utf-8") as f:
            text = f.read()
        anchor = "| n4 | desk | nd-07.lab.invalid |\n"
        row = "| n20 | added | parser-9.lab.invalid |\n"
        self.assertIn(anchor, text)
        with open(self.schema, "w", encoding="utf-8") as f:
            f.write(text.replace(anchor, anchor + row, 1))
        self.assertTrue(D.is_lan("Hosts: parser-9\n"))
        self.assertIn("parser9", D.lan_roles())
        self.assertNotIn("parseroutside", D.lan_roles())
        self.assertFalse(D.is_lan("Hosts: someotherbox\n"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
