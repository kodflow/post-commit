The runner-template stubs of this repository differ from the central copies in
[kodflow/post-commit](https://github.com/kodflow/post-commit/tree/main/stub/runner-template).
This pull request writes the central copies verbatim.

### What a stub is

A few lines `on: repository_dispatch` that call a reusable workflow of
[kodflow/runner-template](https://github.com/kodflow/runner-template) at a
pinned commit. All the logic lives there; the run, its log, its artifacts, the
`private-source` environment and the kodflow-ci App key are this repository's.
Nothing in a stub is specific to this repository but the list of what it runs
and sweeps, and that list is central too.

### Before merging

- The pin is a full SHA on kodflow/runner-template's `main`. Every stub of
  every owner carries the same one.
- The workflow file names and `event_type`s are the ones the private callers
  already dispatch and look runs up by, so no caller changes with this merge.
- A local change to a stub is reverted by the next nightly `enforce` run:
  change `stub/runner-template/` in kodflow/post-commit instead.
