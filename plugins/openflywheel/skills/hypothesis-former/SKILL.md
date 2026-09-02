---
name: hypothesis-former
description: Record one OpenFlywheel harness hypothesis from exact supported failure-pattern and diagnosis receipts in a prepared experiment. Use after exact pattern mining and before any candidate edit; do not use with inconclusive evidence or to modify harness files.
---

# Hypothesis Former

Use only selected pattern IDs and their exact diagnosis artifact IDs from the same prepared
experiment. Keep the global diagnosis set at fifty IDs or fewer. Read the compact diagnoses,
then propose one bounded statement, rationale, expected effect, regression-risk list, component
taxonomy, and one or more exact target paths.

Call `record_hypothesis` once with the prepared worktree, experiment ID, current initialization
commit, exact pattern-to-diagnosis assignments, and proposed target. The tool recomputes the
patterns and rejects missing, extra, misassigned, or inconclusive evidence. It also proves each
target is exactly present in the authoritative editable allowlist; `component_kind` describes the
change and never grants path access.

Retain the returned hypothesis ID and artifact path. Stop before editing any harness file. On an
MCP timeout, retry the identical request once because publication is idempotent; if the retry
also times out, stop with unknown operation status. Do not query Langfuse, rewrite diagnoses,
broaden target paths, infer support, or begin candidate work while following this skill.
