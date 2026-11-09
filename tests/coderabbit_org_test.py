"""Tests for the CodeRabbit organisation material: coderabbit/ (the Organization
Settings and their baseline) against the pinned schema, and the parsers of
scripts/coderabbit_metrics.py.

Run by tests/run.sh with the python that has PyYAML. When the `jsonschema`
package is importable too, the settings are also held to it.
"""
import contextlib
import glob
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import coderabbit as cr  # noqa: E402
import coderabbit_metrics as metrics  # noqa: E402

SCHEMA = os.path.join(ROOT, "stub", "coderabbit", "schema.v2.json")
CENTRAL = os.path.join(ROOT, "stub", "coderabbit", "central.yaml")
SETTINGS = sorted(glob.glob(os.path.join(ROOT, "coderabbit", "**", "*.yaml"), recursive=True))

try:
    import jsonschema
except ImportError:  # optional on a laptop
    jsonschema = None

SUMMARY = ('<!-- final_review_risk_coverage:{"sourceCommitId":"90964109b196","coveredCommitId":'
           '"90964109b196e06977ae6384b80ba12c0c2eb057","kind":"reviewed"} -->')
PAUSED = ("<!-- This is an auto-generated comment: review paused by coderabbit.ai -->\n"
          "> - Last reviewed commit: a6147257b196e06977ae6384b80ba12c0c2eb057\n")
LIMITED = ("<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n"
           "> ## Review limit reached\n> **Plan**: Pro\n")
ALLOW_ATTEMPTS = ("**Included review availability:** This review used your included allowance. 0 included reviews "
                  "remain after this review. Your included PR review attempts over the past 7 days set your "
                  "current allowance at 1 review per hour.")
ALLOW_PLAN = ("**Included review availability:** This review used your included allowance. Your plan provides "
              "up to 1 included review per hour; 0 remain after this review.")


class OrgSettings(unittest.TestCase):
    def test_settings_present(self):
        names = [os.path.relpath(f, ROOT) for f in SETTINGS]
        self.assertIn(os.path.join("coderabbit", "settings.yaml"), names)
        self.assertIn(os.path.join("coderabbit", "baseline", "settings.yaml"), names)

    def test_settings_valid_against_pinned_schema(self):
        v = cr.Validator(SCHEMA)
        for f in SETTINGS:
            with self.subTest(f=os.path.relpath(f, ROOT)):
                self.assertEqual(v.errors(cr.load_file(f)), [])

    @unittest.skipIf(jsonschema is None, "jsonschema not installed")
    def test_settings_valid_for_jsonschema(self):
        with open(SCHEMA, encoding="utf-8") as fh:
            ref = jsonschema.Draft202012Validator(json.load(fh))
        for f in SETTINGS:
            with self.subTest(f=os.path.relpath(f, ROOT)):
                self.assertEqual([e.message for e in ref.iter_errors(cr.load_file(f))], [])


class Exclusions(unittest.TestCase):
    """metrics.py stays stdlib-only, so it keeps its own copy of the two lists:
    this holds the copy to the central part CodeRabbit actually reads."""

    def test_same_lists_as_central(self):
        auto = cr.load_file(CENTRAL)["reviews"]["auto_review"]
        self.assertEqual(sorted(metrics.norm_login(u) for u in auto["ignore_usernames"]),
                         sorted(metrics.EXCLUDED_AUTHORS))
        self.assertEqual(list(auto["ignore_title_keywords"]), list(metrics.EXCLUDED_TITLES))


class Output(unittest.TestCase):
    def _run(self, *argv):
        def fake(owner, since, until, out):
            out.append(f"## {owner}\n")
        with mock.patch.object(metrics, "report_owner", fake), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = metrics.main(["--since", "2026-11-01", "--until", "2026-11-08", "--owner", "kodflow", *argv])
        return rc, buf.getvalue()

    def test_default_dir_is_ignored_build(self):
        self.assertEqual(os.path.relpath(metrics.OUT_DIR, ROOT), os.path.join("build", "coderabbit", "reports"))
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn("build/", fh.read().split())

    def test_writes_report_file(self):
        with tempfile.TemporaryDirectory() as d:
            rc, printed = self._run("--out-dir", d)
            path = os.path.join(d, "metrics-2026-11-01_2026-11-08.md")
            self.assertEqual((rc, printed.strip()), (0, path))
            with open(path, encoding="utf-8") as fh:
                self.assertIn("## kodflow", fh.read())

    def test_stdout(self):
        rc, printed = self._run("--stdout")
        self.assertEqual(rc, 0)
        self.assertIn("# CodeRabbit metrics, 2026-11-01 to 2026-11-08", printed)


class Parsers(unittest.TestCase):
    def test_last_reviewed_marker(self):
        self.assertEqual(metrics.last_reviewed([SUMMARY]), "90964109b196e06977ae6384b80ba12c0c2eb057")

    def test_last_reviewed_text_fallback(self):
        self.assertEqual(metrics.last_reviewed([PAUSED]), "a6147257b196e06977ae6384b80ba12c0c2eb057")

    def test_last_reviewed_unknown(self):
        self.assertIsNone(metrics.last_reviewed(["nothing here"]))

    def test_allowance_attempts_form(self):
        plan, rate, raw, ok = metrics.parse_allowance("- **Plan**: Team\n" + ALLOW_ATTEMPTS)
        self.assertEqual((plan, rate, ok), ("Team", 1, True))
        self.assertIn("Included review availability", raw)

    def test_allowance_plan_form(self):
        self.assertEqual(metrics.parse_allowance(ALLOW_PLAN)[1:4:2], (1, True))

    def test_allowance_unparsed(self):
        plan, rate, raw, ok = metrics.parse_allowance("**Included review availability:** something new")
        self.assertEqual((rate, ok), (None, False))

    def test_exclusion(self):
        self.assertTrue(metrics.is_excluded("kodflow-ci", "x"))
        self.assertTrue(metrics.is_excluded("kodflow-ci[bot]", "x"))
        self.assertTrue(metrics.is_excluded("a", "CI: Add the mandatory post-commit gate (#3)"))
        self.assertTrue(metrics.is_excluded("a", "CI(coderabbit): Sync .coderabbit.yaml with the central template"))
        self.assertFalse(metrics.is_excluded("a", "ci(post-commit): fix the gate concurrency group"))

    def _pr(self, bodies, state="MERGED", head="90964109b196e06977ae6384b80ba12c0c2eb057"):
        nodes = [{"author": {"login": "coderabbitai"}, "body": b} for b in bodies]
        return {"comments": {"nodes": nodes}, "reviews": {"nodes": []}, "state": state, "headRefOid": head}

    def test_analyse_covered_and_refusals(self):
        a = metrics.analyse(self._pr([SUMMARY, LIMITED, PAUSED.replace("a6147257", "00000000")]))
        self.assertEqual((a["covered"], a["refusals"], a["pauses"], a["plan"]), ("yes", 1, 1, "Pro"))

    def test_analyse_not_covered_and_unknown(self):
        self.assertEqual(metrics.analyse(self._pr([SUMMARY], head="f" * 40))["covered"], "no")
        self.assertEqual(metrics.analyse(self._pr(["hi"]))["covered"], "unknown")
        self.assertIsNone(metrics.analyse(self._pr([SUMMARY], state="OPEN"))["covered"])


if __name__ == "__main__":
    unittest.main()
