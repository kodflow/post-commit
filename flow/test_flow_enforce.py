import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts/enforce.sh"


@unittest.skipUnless(SCRIPT.exists(), "requires repository scripts")
class EnforceAuditTests(unittest.TestCase):
    def test_flow_audit_failure_is_not_hidden_by_healthy_legacy_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gh").write_text('''#!/bin/bash
case "$*" in
  "repo view "*) echo main ;;
  *rulesets/1*) echo 0 ;;
  *rulesets*) echo '[{"name":"post-commit","id":1}]' ;;
  *) echo public ;;
esac
''')
            (root / "python3").write_text('''#!/bin/bash
if [ "$1" = -c ]; then exit 0; fi
printf '%s\\n' "$*" >> "$FLOW_AUDIT_LOG"
exit "$FLOW_AUDIT_RESULT"
''')
            for name in ("gh", "python3"):
                (root / name).chmod(0o755)
            log = root / "calls"
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"],
                       FLOW_AUDIT_LOG=str(log), FLOW_AUDIT_RESULT="1")
            failed = subprocess.run(["bash", str(SCRIPT), "--audit", "--flow", "kodflow/post-commit"], env=env, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertIn("flow audit failed", failed.stdout + failed.stderr)
            self.assertIn("--audit -- kodflow/post-commit", log.read_text())
            env["FLOW_AUDIT_RESULT"] = "0"
            passed = subprocess.run(["bash", str(SCRIPT), "--audit", "--flow", "kodflow/post-commit"], env=env, capture_output=True, text=True)
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
