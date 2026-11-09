"""Tests for scripts/coderabbit.py: YAML reading, the merge, the schema check,
the migration of the fleet's real hand-written files, and idempotence.

Run by tests/run.sh with the python that has PyYAML. When the `jsonschema`
package is importable too, every verdict of the built-in validator is held to
it (the "oracle" tests); tests/run.sh requires that they ran in CI.
"""
import copy
import glob
import os
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import coderabbit as cr  # noqa: E402

STUB = os.path.join(ROOT, "stub", "coderabbit")
FIX = os.path.join(ROOT, "tests", "fixtures", "coderabbit")
SCHEMA = os.path.join(STUB, "schema.v2.json")
TITLES = [
    "ci(post-commit): sync the gate workflow with the central stub",
    "ci(runner-template): sync the stubs with kodflow/post-commit",
    "ci: add the mandatory post-commit gate",
    "ci: run every job on the self-hosted kodflow-runner",
]

try:
    import jsonschema
except ImportError:  # the oracle is optional on a laptop, required in CI
    jsonschema = None


def text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def fixtures():
    """(owner, file name, text) of every real hand-written file kept here."""
    for path in sorted(glob.glob(os.path.join(FIX, "*.y*ml"))):
        name = os.path.basename(path)
        yield name.split("-", 1)[0], name, text(path)


def run_plan(owner, files):
    """plan() over a default branch carrying `files` ({name: text})."""
    with tempfile.TemporaryDirectory() as d:
        for name, content in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(content)
        return cr.plan(cr.Central(STUB, owner), d)


class Yaml12(unittest.TestCase):
    def test_off_and_dates_stay_strings(self):
        d = cr.load_text("a: off\nb: yes\nc: 2026-11-09\nd: on\n", "t")
        self.assertEqual(d, {"a": "off", "b": "yes", "c": "2026-11-09", "d": "on"})

    def test_core_scalars(self):
        d = cr.load_text("a: true\nb: 012\nc: 0o17\nd: 0x1f\ne: 1.5\nf: ~\ng: .inf\n", "t")
        self.assertEqual(d, {"a": True, "b": 12, "c": 15, "d": 31, "e": 1.5, "f": None, "g": float("inf")})

    def test_duplicate_key_refused(self):
        with self.assertRaises(cr.Refused):
            cr.load_text("reviews:\n  profile: chill\nreviews:\n  profile: assertive\n", "t")

    def test_not_a_mapping_refused(self):
        with self.assertRaises(cr.Refused):
            cr.load_text("- a\n- b\n", "t")

    def test_dump_reads_back_the_same_under_both_versions(self):
        data = {"s": ["off", "yes", "0o17", "2026-11-09", "null", "012", "~", "true", "x: y", "#c"],
                "multi": "line one\nline two\n", "u": "é → 🐰", "n": 3, "b": False}
        out = cr.dump(data)
        self.assertEqual(yaml.load(out, Loader=cr.Loader), data)
        self.assertEqual(yaml.safe_load(out), data)
        self.assertIn("multi: |", out)


class Overlay(unittest.TestCase):
    def test_scalar_managed_wins(self):
        self.assertEqual(cr.overlay({"a": 1, "b": 2}, {"a": 9}), {"a": 9, "b": 2})

    def test_list_central_first_local_after_deduplicated(self):
        got = cr.overlay({"l": ["WIP", "x", "WIP2"]}, {"l": ["x", "c"]})
        self.assertEqual(got, {"l": ["x", "c", "WIP", "WIP2"]})

    def test_mapping_keeps_local_order_appends_managed(self):
        got = cr.overlay({"z": 1, "r": {"b": 1, "a": 2}}, {"r": {"c": 3}, "n": 4})
        self.assertEqual(list(got), ["z", "r", "n"])
        self.assertEqual(list(got["r"]), ["b", "a", "c"])

    def test_type_conflict_managed_wins(self):
        self.assertEqual(cr.overlay({"r": "scalar"}, {"r": {"a": 1}}), {"r": {"a": 1}})
        self.assertEqual(cr.overlay({"l": "x"}, {"l": ["y"]}), {"l": ["y"]})

    def test_inputs_untouched(self):
        base, managed = {"l": ["a"], "m": {"k": 1}}, {"l": ["b"], "m": {"j": 2}}
        b0, m0 = copy.deepcopy(base), copy.deepcopy(managed)
        cr.overlay(base, managed)
        self.assertEqual((base, managed), (b0, m0))


class CentralPart(unittest.TestCase):
    def test_every_owner_gets_the_exclusion(self):
        for owner in ("kodflow", "kitsunium", "supervizio"):
            m = cr.Central(STUB, owner).managed
            self.assertEqual(m["reviews"]["auto_review"]["ignore_usernames"], ["kodflow-ci[bot]"])
            self.assertEqual(m["reviews"]["auto_review"]["ignore_title_keywords"], TITLES)

    def test_kitsunium_alone_refuses_author_approval(self):
        self.assertIs(cr.Central(STUB, "kitsunium").managed["reviews"]["auto_approvals"]
                      ["allow_author_approval"], False)
        for owner in ("kodflow", "supervizio"):
            self.assertNotIn("auto_approvals", cr.Central(STUB, owner).managed["reviews"])

    def test_central_files_are_valid_against_the_schema(self):
        v = cr.Validator(SCHEMA)
        for f in ("central.yaml", "defaults.yaml", "owners/kitsunium.yaml"):
            self.assertEqual(v.errors(cr.load_file(os.path.join(STUB, f))), [], f)

    def test_header_names_the_central_blobs_not_the_head(self):
        out, _ = cr.Central(STUB, "kitsunium").render(None)
        head = out.split("\n")[:6]
        self.assertTrue(head[1].startswith(cr.GENERATED))
        self.assertTrue(any("central.yaml@" in ln and "owners/kitsunium.yaml@" in ln for ln in head))


class Schema(unittest.TestCase):
    BAD = [
        {"no_such_key": 1},
        {"reviews": {"profile": "loud"}},
        {"tone_instructions": "x" * 251},
        {"reviews": {"auto_review": {"ignore_usernames": "kodflow-ci[bot]"}}},
        {"reviews": {"path_instructions": [{"path": "**"}]}},
        {"inheritance": "yes"},
        {"reviews": {"auto_approvals": {"no_such_key": True}}},
    ]

    def test_refuses(self):
        v = cr.Validator(SCHEMA)
        for doc in self.BAD:
            self.assertNotEqual(v.errors(doc), [], doc)

    def test_unknown_schema_keyword_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"type": "object", "oneOf": [{"type": "object"}]}')
        try:
            with self.assertRaises(cr.Refused):
                cr.Validator(fh.name)
        finally:
            os.unlink(fh.name)

    @unittest.skipIf(jsonschema is None, "no jsonschema package")
    def test_oracle_same_verdicts_as_jsonschema(self):
        """Every document here, valid and invalid, gets the same verdict from
        the built-in validator and from the jsonschema package."""
        import json
        with open(SCHEMA, encoding="utf-8") as fh:
            ref = jsonschema.Draft202012Validator(json.load(fh))
        mine = cr.Validator(SCHEMA)
        docs = list(self.BAD)
        for owner, name, t in fixtures():
            d = cr.load_text(t, name)
            docs.append(d)
            docs.append(cr.overlay(d, cr.Central(STUB, owner).managed))
        for owner in ("kodflow", "kitsunium"):
            docs.append(cr.Central(STUB, owner).render(None)[1])
        docs += [{"reviews": {"auto_review": {"ignore_title_keywords": [1]}}},
                 {"reviews": {"labeling_instructions": [{"label": "x", "instructions": "y"}]}},
                 {"reviews": {"pre_merge_checks": {"docstrings": {"mode": "off"}}}},
                 {"reviews": {"pre_merge_checks": {"docstrings": {"mode": False}}}},
                 {"knowledge_base": ["a list where an object goes"]}]
        valid = invalid = 0
        for d in docs:
            want = not list(ref.iter_errors(d))
            self.assertEqual(not mine.errors(d), want, d if len(str(d)) < 300 else str(d)[:300])
            valid += want
            invalid += not want
        self.assertGreater(valid, 0)
        self.assertGreater(invalid, 0)
        print(f"\n  oracle: jsonschema agrees on {valid} valid and {invalid} invalid documents",
              file=sys.stderr)


class Migration(unittest.TestCase):
    """The real files: the first sync moves them to .coderabbit.local.yaml and
    changes nothing CodeRabbit reads but the managed keys."""

    def test_every_fixture(self):
        self.assertGreaterEqual(len(list(fixtures())), 6)
        for owner, name, t in fixtures():
            with self.subTest(name):
                deployed = cr.YML if name.endswith(".yml") else cr.YAML
                kind, write, delete, body, _ = run_plan(owner, {deployed: t})
                self.assertEqual(kind, "migrate")
                self.assertEqual(set(write), {cr.LOCAL, cr.YAML})
                self.assertEqual(delete, [cr.YML] if deployed == cr.YML else [])
                central = cr.Central(STUB, owner)
                original = cr.load_text(t, name)
                rendered = cr.load_text(write[cr.YAML], "rendered")
                # Effective configuration: the original with the managed part on
                # top, nothing else, and the difference only on managed keys.
                self.assertEqual(rendered, cr.overlay(original, central.managed))
                changed = {p for p, _ in cr.diff(original, rendered)}
                self.assertLessEqual(changed, central.managed_leaves)
                # The local file is the repository's whole part: it renders to the
                # same file, and the next run finds nothing to do.
                self.assertEqual(central.render(write[cr.LOCAL])[0], write[cr.YAML])
                again = run_plan(owner, {cr.LOCAL: write[cr.LOCAL], cr.YAML: write[cr.YAML]})
                self.assertEqual(again[0], "present")
                self.assertEqual(again[1], {})
                # Comments survive: every comment line of the original but the
                # exclusion's own is in the local file.
                comments = [ln.strip() for ln in t.split("\n") if ln.strip().startswith("#")]
                local = write[cr.LOCAL]
                for c in comments:
                    if c not in cr.LEGACY_COMMENT:
                        self.assertIn(c, local)
                # The hand-written exclusion is gone from the local file.
                for title in TITLES:
                    self.assertNotIn(title, local)
                self.assertIn("### What changes in the configuration CodeRabbit reads", body)

    def test_already_excluded_files_change_nothing_but_the_order(self):
        _, write, _, body, _ = run_plan("kitsunium", {cr.YAML: text(os.path.join(FIX, "kitsunium-sdk.yaml"))})
        self.assertIn("- nothing: the configuration CodeRabbit reads is the same", body)
        _, _, _, body, _ = run_plan("kodflow", {cr.YAML: text(os.path.join(FIX, "kodflow-ktn-linter-excerpt.yaml"))})
        self.assertIn("`reviews.auto_review.ignore_title_keywords`: same items, another order", body)
        local = run_plan("kodflow", {cr.YAML: text(os.path.join(FIX, "kodflow-ktn-linter-excerpt.yaml"))})[1][cr.LOCAL]
        kw = cr.load_text(local, "l")["reviews"]["auto_review"]["ignore_title_keywords"]
        self.assertEqual(kw, ["WIP", "DO NOT MERGE", "DRAFT"])
        self.assertNotIn("ignore_usernames", cr.load_text(local, "l")["reviews"]["auto_review"])

    def test_forgejo_raft_list_emptied_key_dropped(self):
        local = run_plan("kodflow", {cr.YAML: text(os.path.join(FIX, "kodflow-forgejo-raft.yaml"))})[1][cr.LOCAL]
        self.assertEqual(cr.load_text(local, "l"),
                         {"reviews": {"auto_review": {"base_branches": ["forgejo", "feat/.*", "docs/.*"]}}})

    def test_file_without_exclusion_gains_it(self):
        _, write, delete, body, _ = run_plan(
            "kodflow", {cr.YML: text(os.path.join(FIX, "kodflow-terraform-provider-n8n.yml"))})
        self.assertIn('added "kodflow-ci[bot]"', body)
        self.assertIn(".coderabbit.yml is removed", body)
        self.assertEqual(delete, [cr.YML])

    def test_strip_that_would_change_meaning_keeps_the_file_verbatim(self):
        hand = 'reviews:\n  auto_review:\n    base_branches:\n      - "kodflow-ci[bot]"\n'
        local = cr.migrate(cr.Central(STUB, "kodflow"), hand)
        self.assertTrue(local.endswith(hand))

    def test_yaml_and_yml_both_hand_written_refused(self):
        with self.assertRaises(cr.Refused):
            run_plan("kodflow", {cr.YAML: "language: en-US\n", cr.YML: "language: fr\n"})

    def test_invalid_hand_file_refused(self):
        with self.assertRaises(cr.Refused):
            run_plan("kodflow", {cr.YAML: "knowledge_base:\n  - a list\n"})
        with self.assertRaises(cr.Refused):
            run_plan("kodflow", {cr.YAML: "reviews: [unclosed\n"})


class Lifecycle(unittest.TestCase):
    def test_create_then_present(self):
        kind, write, delete, body, _ = run_plan("supervizio", {})
        self.assertEqual((kind, set(write), delete), ("create", {cr.YAML}, []))
        data = cr.load_text(write[cr.YAML], "r")
        self.assertIs(data["inheritance"], True)
        self.assertIn("There was no file before", body)
        self.assertEqual(run_plan("supervizio", {cr.YAML: write[cr.YAML]})[0], "present")

    def test_local_edit_is_a_sync(self):
        local = "reviews:\n  profile: chill\n"
        rendered = cr.Central(STUB, "kodflow").render(local)[0]
        kind, write, _, body, _ = run_plan("kodflow", {cr.LOCAL: "reviews:\n  profile: assertive\n",
                                                       cr.YAML: rendered})
        self.assertEqual((kind, set(write)), ("sync", {cr.YAML}))
        self.assertIn('`reviews.profile`: changed from "chill" to "assertive"', body)

    def test_central_change_is_a_sync(self):
        stale = cr.Central(STUB, "kodflow").render(None)[0].replace(
            "    - kodflow-ci[bot]\n", "")
        self.assertEqual(run_plan("kodflow", {cr.YAML: stale})[0], "sync")

    def test_hand_edited_rendering_is_replaced_and_said_so(self):
        local = "reviews:\n  profile: chill\n"
        edited = "reviews:\n  profile: assertive\n"
        kind, write, _, _, note = run_plan("kodflow", {cr.LOCAL: local, cr.YAML: edited})
        self.assertEqual(kind, "sync")
        self.assertIn("edited by hand", note)

    def test_local_with_legacy_yml_deletes_it(self):
        local = "reviews:\n  profile: chill\n"
        rendered = cr.Central(STUB, "kodflow").render(local)[0]
        kind, write, delete, _, _ = run_plan("kodflow", {cr.LOCAL: local, cr.YAML: rendered,
                                                         cr.YML: "language: fr\n"})
        self.assertEqual((kind, write, delete), ("sync", {}, [cr.YML]))

    def test_local_cannot_remove_a_managed_item(self):
        local = "reviews:\n  auto_review:\n    ignore_usernames: []\n    ignore_title_keywords: [WIP]\n"
        data = cr.load_text(cr.Central(STUB, "kodflow").render(local)[0], "r")
        self.assertEqual(data["reviews"]["auto_review"]["ignore_usernames"], ["kodflow-ci[bot]"])
        self.assertEqual(data["reviews"]["auto_review"]["ignore_title_keywords"], TITLES + ["WIP"])

    def test_local_invalid_against_schema_refused(self):
        with self.assertRaises(cr.Refused):
            run_plan("kodflow", {cr.LOCAL: "reviews:\n  profile: loud\n"})

    def test_rendering_is_deterministic(self):
        local = text(os.path.join(FIX, "kodflow-view.yaml"))
        a = cr.Central(STUB, "kodflow").render(local)[0]
        b = cr.Central(STUB, "kodflow").render(local)[0]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=1)
