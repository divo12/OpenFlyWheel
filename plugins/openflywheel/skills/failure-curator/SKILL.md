---
name: failure-curator
description: Group a completed bounded set of recorded failure diagnoses into evidence-bound cross-task patterns before forming a harness hypothesis. Use after failure-miner has recorded every verifier-backed failure; do not query traces, edit the harness, or evaluate candidates.
---

# Failure Curator

Turn only the retained `record_failure` receipt IDs produced for the current completed baseline
or candidate run into a compact cross-task debugger overview. Do not glob or enumerate
`.workspace/failures/`; it may also contain artifacts from earlier runs. Langfuse remains the
source of trace content; read only the current run's recorded diagnosis artifacts in this phase.

## Curate

1. Read every retained failure artifact for the run, up to the tool's 50-artifact bound. If
   the run exceeds that bound, stop and report that complete curation is unavailable.
2. Defer every inconclusive diagnosis with the specific evidence or recurrence gap. A
   supported diagnosis may also be deferred when it has no matching failure from another task.
3. Group supported diagnoses only when at least two distinct task IDs share the same causal
   mechanism and top-level `issue_type`. Shared entities, words, tools, or symptoms alone do
   not establish a pattern.
4. Give each group a stable lowercase `pattern_key`, concise title, causal mechanism, general
   prevention mechanism, and exactly one most relevant harness `target_component`. Keep the
   prevention mechanism structural rather than task-specific; do not describe a file edit yet.
5. Assign every source artifact exactly once, either to one group or one deferred entry.

Call `record_failure_curation` once with the prepared worktree as `workspace_root`, the full
source artifact ID set, the proposed groups, and deferred entries. Retain the returned
`.workspace/failure-curations/<curation-id>.json` path for the hypothesis phase.

Do not call trace tools, copy trace content, invent missing causal evidence, combine different
failure types, form a repair hypothesis, edit the harness, run a benchmark, or promote a change
while following this skill.
