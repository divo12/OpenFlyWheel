---
name: hypothesis-former
description: Record one OpenFlywheel harness hypothesis from one curated failure group and its exact supported pattern and diagnosis receipts. Use after failure curation and before any candidate edit; do not use with incomplete evidence or to modify harness files.
---

# Hypothesis Former

Select one group from the retained failure-curation receipt. Use every diagnosis artifact ID in
that group, partitioned under its exact mined pattern IDs; do not add or omit IDs. Keep the global
diagnosis set at fifty IDs or fewer. Read the compact diagnoses, then propose one bounded
statement, rationale, expected effect, regression-risk list, component taxonomy, and one or more
exact target paths within the group's target component.

Call `record_hypothesis` once with the prepared worktree, experiment ID, current initialization
commit, curation ID, selected curation group ID, exact pattern-to-diagnosis assignments, and
proposed target. The tool reloads the curation, requires the complete selected group, recomputes
the patterns, and rejects missing, extra, misassigned, or inconclusive evidence. It also proves
each target is exactly present in the authoritative editable allowlist; `component_kind` must
match the curated component and never grants path access.

Retain the returned hypothesis ID and artifact path. Stop before editing any harness file. On an
MCP timeout, retry the identical request once because publication is idempotent; if the retry
also times out, stop with unknown operation status. Do not query Langfuse, rewrite diagnoses,
broaden target paths, infer support, or begin candidate work while following this skill.
