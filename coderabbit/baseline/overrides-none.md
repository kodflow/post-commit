# Baseline: no global override on 08/11/2026

State of the CodeRabbit dashboard **before any override**: no Organization global override was set for kodflow,
kitsunium or supervizio. The only dashboard text was the Organization Settings
(layer 5), kept verbatim in [`settings.yaml`](settings.yaml).

Layer 1 is no longer used: the common lists now come from kodflow/post-commit
into each repository's `.coderabbit.yaml`. Should a global override ever be set
again, emptying the field returns to this state.

## What is known of each owner's plan

| Owner | Plan | Source |
|---|---|---|
| kodflow | Team; adaptive allowance | seen on forgejo-raft#8 |
| kitsunium | "Plan: Advanced", "up to 1 included review per hour" | seen on kitsunium/sdk#1314 |
| supervizio | unknown | to be read at its gate (`@coderabbitai rate limit`) |
