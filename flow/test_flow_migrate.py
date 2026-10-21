import unittest

from flow_migrate import combine_workflows, configure_reviewers, migrate_workflow, parse_workflow


SOURCE = """name: CI
on:
  push:
    branches: [main]
  pull_request:
    paths: ['src/**']
  workflow_dispatch:
permissions:
  contents: read
env:
  TOOL_VERSION: '1.2'
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: make lint
  unit:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        lane: [test, race]
    steps:
      - run: make test
  e2e:
    needs: [lint, unit]
    runs-on: special-runner
    steps:
      - run: make e2e
"""


def spec():
    return {"advisory_jobs": ["lint", "unit"], "advisory_matrix": {"unit": {"lane": ["test"]}},
            "lanes": {"lint": ["lint"], "unit": ["unit"], "heavy": ["e2e"]}}


class MigrationTests(unittest.TestCase):
    def test_release_dependency_can_retain_original_push_qualification(self):
        profile = spec()
        profile["preserve_push"] = True
        full, advisory = map(parse_workflow, migrate_workflow(SOURCE, profile))
        self.assertEqual(full["on"]["push"], {"branches": ["main"]})
        self.assertNotIn("push", advisory["on"])

    def test_on_key_is_preserved_as_string(self):
        parsed = parse_workflow(SOURCE)
        self.assertIn("on", parsed)
        self.assertNotIn(True, parsed)

    def test_central_repo_governance_survives_bootstrap_and_advisory_split(self):
        source = SOURCE + "  post-commit:\n    runs-on: ubuntu-latest\n    steps: [{uses: './'}]\n"
        profile = spec()
        profile["advisory_jobs"].append("post-commit")
        full, advisory = map(parse_workflow, migrate_workflow(source, profile))
        self.assertNotIn("flow-candidate", full["jobs"]["post-commit"].get("needs", []))
        self.assertNotIn("if", full["jobs"]["post-commit"])
        self.assertIn("post-commit", advisory["jobs"])

    def test_full_qualification_only_on_main_pr_and_manual(self):
        full, advisory = map(parse_workflow, migrate_workflow(SOURCE, spec()))
        self.assertNotIn("push", full["on"])
        self.assertIn("workflow_dispatch", full["on"])
        self.assertEqual(full["on"]["pull_request"]["branches"], ["main"])
        self.assertNotIn("paths", full["on"]["pull_request"])
        self.assertEqual(advisory["on"]["pull_request"]["branches"], ["develop"])
        for job in full["jobs"].values():
            self.assertIn("github.head_ref == 'develop'", job["if"])
            self.assertIn("head.repo.full_name == github.repository", job["if"])

    def test_actual_commands_and_environment_preserved(self):
        full, advisory = map(parse_workflow, migrate_workflow(SOURCE, spec()))
        self.assertEqual(full["jobs"]["e2e"]["steps"][0]["run"], "make e2e")
        self.assertEqual(advisory["env"]["TOOL_VERSION"], "1.2")
        self.assertNotIn("e2e", advisory["jobs"])
        self.assertEqual(advisory["jobs"]["unit"]["strategy"]["matrix"]["lane"], ["test"])
        self.assertEqual(full["jobs"]["unit"]["strategy"]["matrix"]["lane"], ["test", "race"])

    def test_failed_or_skipped_jobs_not_hidden_by_successful_summary(self):
        full, _ = map(parse_workflow, migrate_workflow(SOURCE, spec()))
        for lane in ("lint", "unit", "heavy"):
            job = full["jobs"]["ci-" + lane]
            self.assertIn("always()", job["if"])
            self.assertIn("!= 'success'", job["steps"][0]["run"])

    def test_heavy_prerequisite_not_silently_added_to_develop(self):
        source = SOURCE.replace("  unit:\n    runs-on:", "  unit:\n    needs: [e2e]\n    runs-on:")
        with self.assertRaisesRegex(ValueError, "not approved"):
            migrate_workflow(source, spec())

    def test_advisory_matrix_cannot_introduce_new_work(self):
        profile = spec()
        profile["advisory_matrix"]["unit"]["lane"] = ["new-expensive-lane"]
        with self.assertRaises(ValueError):
            migrate_workflow(SOURCE, profile)

    def test_standalone_e2e_can_join_full_graph_without_running_on_develop(self):
        extra = "name: E2E\non: push\nenv:\n  E2E_MODE: real\njobs:\n  setup:\n    runs-on: special\n    steps: [{run: make setup}]\n  test:\n    needs: setup\n    runs-on: special\n    steps: [{run: 'echo ${{ needs.setup.outputs.path }}'}]\n"
        combined, added = combine_workflows(SOURCE, [extra])
        profile = spec()
        profile["lanes"]["heavy"] += added
        full, advisory = map(parse_workflow, migrate_workflow(combined, profile))
        test = full["jobs"]["flow-extra-1-test"]
        self.assertIn("flow-extra-1-setup", test["needs"])
        self.assertIn("needs.flow-extra-1-setup.outputs.path", test["steps"][0]["run"])
        self.assertEqual(test["env"]["E2E_MODE"], "real")
        self.assertNotIn("flow-extra-1-test", advisory["jobs"])

    def test_tag_publication_event_preserved(self):
        source = SOURCE.replace("branches: [main]", "branches: [main]\n    tags: ['v*']", 1)
        full, _ = map(parse_workflow, migrate_workflow(source, spec()))
        self.assertEqual(full["on"]["push"], {"tags": ["v*"]})

    def test_coderabbit_preserves_authored_rules(self):
        cr, qodo = configure_reviewers("reviews:\n  path_instructions:\n    - path: src/**\n      instructions: Keep layers\n  auto_review:\n    base_branches: ['.*']\n", "")
        import yaml
        parsed = yaml.safe_load(cr)
        self.assertEqual(parsed["reviews"]["auto_review"]["base_branches"], [])
        self.assertEqual(parsed["reviews"]["path_instructions"][0]["instructions"], "Keep layers")
        self.assertIn('"^(?!main$).*"', qodo)
        self.assertFalse(parsed["reviews"]["auto_review"]["enabled"])

    def test_qodo_preserves_unrelated_instructions(self):
        original = '[config]\nignore_pr_target_branches = ["^qa$"]\n\n[pr_reviewer]\nextra_instructions = "Keep APIs stable"\n'
        _, qodo = configure_reviewers("", original)
        import tomllib
        parsed = tomllib.loads(qodo)
        self.assertEqual(parsed["config"]["ignore_pr_target_branches"], ["^qa$", "^(?!main$).*"])
        self.assertEqual(parsed["pr_reviewer"]["extra_instructions"], "Keep APIs stable")

    def test_existing_qodo_exclusion_replaced_by_promotion_only_filter(self):
        for text in ('[config]\nignore_pr_target_branches = [".*"]',
                     '[config]\nignore_pr_source_branches = ["develop"]'):
            import tomllib, re
            _, output = configure_reviewers("", text)
            config = tomllib.loads(output)
            self.assertFalse(any(re.search(p, "main") for p in config["config"]["ignore_pr_target_branches"]))
            self.assertFalse(any(re.search(p, "develop") for p in config["config"]["ignore_pr_source_branches"]))
            self.assertEqual(config["github_app"]["pr_commands"], [])

    def test_multiline_qodo_lists_do_not_drop_following_prompts(self):
        import tomllib
        source = '[github_app] # existing settings\npr_commands = [\n  "/agentic_review",\n  "/agentic_describe",\n]\n\n[pr_reviewer]\nextra_instructions = "Preserve API compatibility"\n'
        _, output = configure_reviewers("", source)
        result = tomllib.loads(output)
        self.assertEqual(result["github_app"]["pr_commands"], [])
        self.assertEqual(result["pr_reviewer"]["extra_instructions"], "Preserve API compatibility")


if __name__ == "__main__":
    unittest.main()
