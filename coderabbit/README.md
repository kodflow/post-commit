# coderabbit/

The CodeRabbit material of the three owners kodflow, kitsunium and supervizio
that is **not** written into the repositories: the Organization Settings, pasted
by hand into the CodeRabbit dashboard, and the state of that dashboard before
any override. What `enforce` writes into every repository's `.coderabbit.yaml`
is the other half, under `stub/coderabbit/` (see the CodeRabbit section of the
top-level README).

| File | What it is |
|---|---|
| `settings.yaml` | the Organization Settings text (layer 5), pasted by hand into the dashboard |
| `baseline/settings.yaml` | the dashboard's Organization Settings as they were on 08/11/2026, before any override |
| `baseline/overrides-none.md` | no global override was set on 08/11/2026; the plans known per owner |
| `../stub/coderabbit/schema.v2.json` | the one pinned copy of CodeRabbit's schema, shared with `enforce` |
| `../scripts/coderabbit_metrics.py` | weekly measurement (stdlib only); tests in `../tests/coderabbit_org_test.py` |

## The layers

- **Layer 5, Organization Settings**: `settings.yaml`. A repository whose own
  `.coderabbit.yaml` says `inheritance: false` **ignores layer 5 entirely**
  (for instance kodflow/status-line, kodflow/forgejo-raft, kodflow/view,
  kitsunium/sdk).
- **Layer 1, global override**: no longer used. The lists every repository
  needs (skip `kodflow-ci[bot]` and the sync titles; kitsunium:
  `allow_author_approval: false`) are `stub/coderabbit/central.yaml` and
  `stub/coderabbit/owners/<owner>.yaml`, written by `enforce`. Never duplicate
  them here: to change them, change `stub/coderabbit/`.

The `Layers:` line CodeRabbit prints in a review tells which layers applied.

## Pasting `settings.yaml`

The dashboard has no API for it, so it is pasted by hand.

1. Merge the change to `main`; note the commit SHA of the file.
2. CodeRabbit dashboard, the owner, Organization Settings: paste the file with
   the line `# post-commit@<sha>` added at the top.
3. Rollback: paste the previous version (`git show <sha>:coderabbit/settings.yaml`),
   or `baseline/settings.yaml`. The state before any override is
   `baseline/overrides-none.md`; for the part `enforce` writes, revert the
   change in `stub/coderabbit/` and let the next enforce run rewrite the
   repositories.

## The relaunch workflow reads this directory

The workflow that re-asks CodeRabbit for the reviews it refused for quota
(`probe.py` of kodflow-hooks, in kodflow/claude-marketplace) runs elsewhere,
next to personal tokens, which a public repository must not hold. It checks
this directory out anonymously and passes it as `--policy-dir`: the probe
reads `settings.yaml` here for a repository without its own `.coderabbit.yaml`,
and `override-<owner>.yaml` when one exists (none does: an empty override is
the intended state). Renaming or moving `settings.yaml` therefore breaks it.

## Checking

CI (`.github/workflows/ci.yml`, through `tests/run.sh`) validates every
`*.yaml` here against the pinned schema, with `scripts/coderabbit.py`'s
built-in validator and, in CI, with the `jsonschema` package too. Locally:

```sh
python3 scripts/coderabbit.py validate --root stub/coderabbit coderabbit/settings.yaml coderabbit/baseline/settings.yaml
python3 -m unittest tests/coderabbit_org_test.py      # needs PyYAML
```

## Measuring

```sh
python3 scripts/coderabbit_metrics.py                 # the last 7 days, every owner
python3 scripts/coderabbit_metrics.py --since 2026-11-01 --until 2026-11-08 --owner kitsunium
```

It needs `gh` authenticated with read access to the owners' pull requests.
The report counts, per owner, author and repository, the eligible pull
requests, those with at least one review, the quota refusals and pauses, and
the merged pull requests whose head CodeRabbit never reviewed. It names every
repository it reads, private ones included, so it is written to
`build/coderabbit/reports/` (ignored by git) and **never committed here**:
keep the reports somewhere private. A change of allowance tier between two
reports makes their comparison inconclusive.

## Checks after a paste

Played in order after each paste; the results of 09/11/2026 are below. A
repository with its own file (`inheritance: false`) ignores layer 5: only its
own file, written by `enforce`, counts.

**Trap**: a test pull request with an empty commit gets "No files to review"
(skipped) whatever its title. A test must always touch a file, or it proves
neither the skip by title nor the review.

| # | Check | Expected |
|---|---|---|
| 1 | `Layers:` of the next review | names the repository's file |
| 2 | Excluded title `ci(post-commit): sync the gate workflow with the central stub`, touching a file | skipped, "Ignore keyword(s) in the title" |
| 3 | `WIP: canary`, touching a file | skipped only where the repository's own keywords list `WIP` |
| 4 | `ci(post-commit): fix the gate concurrency group`, touching a file | reviewed (or refused for quota: not skipped) |
| 5 | A pull request by `kodflow-ci[bot]` | skipped, "author ignored by configuration" |
| 6 | kitsunium: an author approval (`allow_author_approval: false`) | no approval |

Results of 09/11/2026 (test pull requests closed, branches deleted):

- **kitsunium** (kitsunium/sdk): 2 skipped by title
  ([#1322](https://github.com/kitsunium/sdk/pull/1322)); 3 not applicable,
  sdk has no `WIP` keyword, and the empty commit was skipped
  ([#1323](https://github.com/kitsunium/sdk/pull/1323)); 4 not skipped, review
  started then cancelled by closing ([#1325](https://github.com/kitsunium/sdk/pull/1325));
  5 skipped, author ignored ([#1321](https://github.com/kitsunium/sdk/pull/1321));
  1 and 6 not read. Plan: Advanced, "up to 1 included review per hour".
- **kodflow** (a private repository with its own file): 2 and 3 skipped by
  title (the repository's keywords are unioned with the central ones); 4 not
  skipped but refused for quota ("Review limit reached", overage disabled),
  so it proves only that the title is not excluded; 5 skipped, author ignored;
  1 not read. Plan: Team, adaptive allowance.
- **supervizio** (a private repository with its own file): 2 skipped by title;
  3 not applicable (no `WIP` keyword, empty commit); 4 reviewed, summary and
  walkthrough only: the organisation is on the Free plan; 5 SKIPPED (reason
  not readable through the API); 1 not read.
