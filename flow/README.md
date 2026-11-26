# Integration and promotion policy

The existing post-commit identity, secret and content gate stays mandatory.
The flow controller adds the common branch contract while retaining each
project's actual CI commands, runners, matrices and dependencies.

| Target | Validation | Admission |
|---|---|---|
| develop | Selected lint/unit jobs report real results | Red lint/unit is allowed; governance, conflicts, review budget and promotion freeze still block |
| main | Complete existing CI, including lint/unit and heavy tests | Only same-repository develop promotions, successful CI and current review evidence |

The GitHub default branch stays main. Feature PRs mistakenly targeting main are
retargeted to develop. No rebase or up-to-date-base requirement is imposed on
develop; conflicts must be resolved. Promotions use merge commits.

## Admission and correction reserve

Apply `flow:merge` to request a merge. Its latest application must be by an
account with write/maintain/admin permission. The central controller polls the
enrolled fleet every five minutes or runs on demand through flow-control.
GitHub schedules can be delayed; this is not a five-minute SLA.

Only the kodflow-ci App can merge through flow-writer. Its bypass applies only
to that update restriction, never the separate CI/PR rules. The controller
rechecks live SHA values, merge intent and freeze before merging, pins the
requested head SHA in the API, and stops before admitting a second PR from the
same snapshot.

A ready develop → main promotion freezes feature admission. A writer may label
a repair `flow:correction`. Corrections can use the reserve between soft and hard
limits. New functionality stops at the soft boundary. Outside a freeze,
equal/decreasing-volume changes remain possible after that boundary. An already
oversized branch may recover without increasing any dimension, but cannot be
promoted above a hard limit.

Limits cover files, added plus removed lines, total patch bytes and largest
per-file patch. Binary changes count actual blob bytes. Metrics use simulated
merges and PR merge-base semantics, not summed PR statistics. Each reviewer gets
its own exclusions. Initial soft/hard ceilings are 80/100 files, 2000/2500 lines,
64/80 kB total patch and 16/20 kB per-file patch. These are configurable
operational limits, not promises about subscribed vendor allowances.

## Qualification evidence

CI uploads a tiny candidate artifact identifying PR, head, base, merge SHA, run
and attempt. Stale metadata, missing/duplicate summaries, and failed, cancelled,
neutral or skipped required jobs cannot qualify a promotion.

For agent, the controller also waits for the trusted `e2e/runner-template`
aggregate on the exact merge SHA. It verifies the external workflow run and
its freshness against the current CI attempt. Dispatch acceptance is insufficient.
An explicitly skipped dispatch is accepted only when the authored change detector
determined that no family needed building. External checkouts use the merge SHA.

Agent's project graph validator reads the preserved authored workflow under
.github/flow-source; it continues validating project-owned tables and their graph.
The generated wrapper graph is guarded by managed-file hashes and migration tests.
Validator source drift blocks rollout until the pinned profile is reviewed.

After all CI succeeds and the diff fits hard limits, the controller requests
one full CodeRabbit review and one Qodo review per head/base pair. Vendor push
triggers are disabled. Polling does not repeat requests or enable paid overflow.

Silence and green commit statuses do not prove a review. Adapters bind completion
to the current head, require reported file coverage and reject quota, partial,
error and unresolved-findings results. Unknown formats stay pending. CodeRabbit's
format was checked against real reviews. Qodo's documented current-commit and
zero-findings format has fixtures but still needs a positive live validation
after its credits return.

ktn-linter PR 861 exposed CodeRabbit success with description "Review rate
limited", no submitted review, and Qodo billing-blocked. This is covered by tests.
After quota/billing recovery, an operator may request a review manually using
`@coderabbitai full review` or `/agentic_review`; the controller does not retry
paid reviews continuously.

## Deployment

The privileged controller runs centrally here with the existing per-owner App
tokens. Its private key is not copied to targets; target PR code never executes
in the controller. Runtime uses Python stdlib, Git and gh. Migration additionally
uses the pinned PyYAML requirement and Python 3.11 or newer.

```sh
python -m pip install -r flow/requirements.txt
python flow/flow_rollout.py -- kodflow/ktn-linter
python flow/flow_rollout.py --output-dir /tmp/flow-preview -- kodflow/ktn-linter
python flow/flow_rollout.py --apply -- kodflow/ktn-linter
python flow/flow_rollout.py --audit -- kodflow/ktn-linter
```

Dry-run never modifies GitHub. Apply opens a migration PR, preserves authored CI
under .github/flow-source, splits advisory and full CI, and configures reviewers.
Standalone heavy pipelines can join the full graph: SDK cross-platform E2E no
longer needs to run on every feature push. Tag publication and delivery builds
are retained.

SDK explicitly retains its original main-push CI because sdk-release.yml consumes
that successful run before publishing. This temporarily repeats full qualification
after promotion on SDK; removing it requires migrating the release proof first.
Unknown workflow_run consumers block migration instead of losing publication.

Protections are installed only after managed producers exist on both branches.
Develop is created from main when absent; existing branches receive an install
PR first. Organization rules are preferred when accessible, scoped to enrolled
repositories, with repository fallback. Unavailable enforcement is an error.
Unrelated rulesets and existing migration PR edits are preserved.

`scripts/enforce.sh --flow` connects deployment to fleet reconciliation; the
scheduled workflow enables it. Existing callers without the flag keep their
old behavior. Profiles currently cover post-commit, ktn-linter, agent, libprobe
and sdk against inspected workflow blob SHA values. Other repositories report
flow=needs-profile; their job classification is never guessed or called migrated.
Workflow source drift requires a reviewed profile update.

Managed policy/CI changes after enrollment require administrator authorization
through `flow:policy` on their integration PR and promotion. Ordinary feature PRs
cannot silently remove required producers or change review exclusions.

## Checks

```sh
python -m unittest discover -s flow -p 'test_*.py' -v
bash tests/run.sh
shellcheck -S warning scripts/*.sh tests/*.sh
git diff --check
```

Git tests use real temporary repositories and merges. Other tests cover API
pagination, stale candidates, serialized admission, source/matrix preservation,
partial/absent reviews, quota-success statuses, producer readiness and org fallback.
