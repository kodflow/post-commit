import unittest

from flow_api import GitHub, authorized_label, reconcile_rules
from flow_policy import rulesets
from test_flow_policy import example_policy


class FakeAPI:
    def __init__(self):
        self.rules = {}
        self.mutations = []
        self.events = []
        self.permission = "read"

    def pages(self, endpoint):
        if endpoint.endswith("events"):
            return self.events
        return [{"id": identifier, **rule} for identifier, rule in self.rules.items()]

    def request(self, method, endpoint, payload=None):
        if method != "GET":
            self.mutations.append((method, endpoint, payload))
            return {"id": 1}
        if endpoint.endswith("permission"):
            return {"permission": self.permission}
        return self.rules[int(endpoint.rsplit("/", 1)[1])]


class APITests(unittest.TestCase):
    def test_rules_dry_run_never_writes(self):
        api = FakeAPI()
        self.assertEqual(len(reconcile_rules(api, "repos/a/b/rulesets", rulesets(example_policy()))), 3)
        self.assertEqual(api.mutations, [])

    def test_rules_are_idempotent_and_ignore_unrelated_names(self):
        api = FakeAPI()
        desired = rulesets(example_policy())
        api.rules = {index: rule for index, rule in enumerate(desired, 1)}
        api.rules[9] = {"name": "some-other-policy"}
        self.assertEqual(reconcile_rules(api, "repos/a/b/rulesets", desired, apply=True), [])
        self.assertEqual(api.mutations, [])

    def test_disabled_or_bypassable_rule_is_repaired(self):
        api = FakeAPI()
        desired = rulesets(example_policy())
        api.rules = {1: desired[0] | {"enforcement": "disabled"}}
        changes = reconcile_rules(api, "repos/a/b/rulesets", desired, apply=True)
        self.assertEqual(changes[0]["method"], "PUT")
        self.assertEqual(api.mutations[0][2]["enforcement"], "active")

    def test_inherited_rule_cannot_be_overwritten_via_repo_endpoint(self):
        api = FakeAPI()
        desired = rulesets(example_policy())
        api.rules = {1: desired[0] | {"source_type": "Organization"}}
        with self.assertRaises(ValueError):
            reconcile_rules(api, "repos/a/b/rulesets", desired, apply=True)
        self.assertEqual(api.mutations, [])

    def test_correction_label_requires_writer_who_applied_it(self):
        api = FakeAPI()
        pr = {"number": 1, "labels": [{"name": "flow:correction"}]}
        api.events = [{"event": "labeled", "label": {"name": "flow:correction"},
                       "actor": {"login": "maintainer"}}]
        self.assertFalse(authorized_label(api, "a/b", pr, "flow:correction"))
        api.permission = "write"
        self.assertTrue(authorized_label(api, "a/b", pr, "flow:correction"))

    def test_pagination_does_not_stop_after_first_page(self):
        class Paged(GitHub):
            def request(self, method, endpoint, payload=None, **kwargs):
                from urllib.parse import parse_qs, urlsplit
                return list(range(100)) if parse_qs(urlsplit(endpoint).query)["page"] == ["1"] else [100]
        self.assertEqual(len(Paged().pages("repos/a/b/pulls")), 101)


if __name__ == "__main__":
    unittest.main()
