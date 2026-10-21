"""Immutable CI candidate metadata, uploaded before any project code executes."""

CHECKOUT_SHA = "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
UPLOAD_SHA = "cf430e030ddbb5b0abf93d22962f4752f3646cd9"


def candidate_job():
    return {
        "name": "flow-candidate", "runs-on": "ubuntu-latest", "timeout-minutes": 5,
        "permissions": {"contents": "read"},
        "steps": [
            {"uses": "actions/checkout@" + CHECKOUT_SHA,
             "with": {"ref": "${{ github.sha }}", "persist-credentials": False}},
            {"name": "Record the exact validated candidate", "env": {
                "FLOW_PR": "${{ github.event.pull_request.number || 0 }}",
                "FLOW_BASE": "${{ github.event.pull_request.base.sha || github.sha }}",
                "FLOW_HEAD": "${{ github.event.pull_request.head.sha || github.sha }}",
            }, "run": "python3 - <<'PY'\nimport json, os, subprocess\nsha = subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()\nif sha != os.environ['GITHUB_SHA']:\n    raise SystemExit('Checkout is not the event candidate')\nwith open('flow-candidate.json','w') as f:\n    json.dump(dict(version=1, pr=int(os.environ['FLOW_PR']), base=os.environ['FLOW_BASE'], head=os.environ['FLOW_HEAD'], merge=sha, run_id=int(os.environ['GITHUB_RUN_ID']), attempt=int(os.environ['GITHUB_RUN_ATTEMPT'])), f)\nPY\n"},
            {"uses": "actions/upload-artifact@" + UPLOAD_SHA, "with": {
                "name": "flow-candidate-${{ github.run_attempt }}", "path": "flow-candidate.json",
                "retention-days": 3, "if-no-files-found": "error",
            }},
        ],
    }
