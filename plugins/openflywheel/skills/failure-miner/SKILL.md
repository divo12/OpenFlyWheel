---
name: failure-miner
description: Diagnose one authoritative failed agent outcome from bounded Langfuse evidence and record the supported or inconclusive result in the prepared harness workspace. Use after record_outcome returns a fail receipt; do not use for outcome judging, cross-trace pattern mining, or harness edits.
---

# Failure Miner

Diagnose why one verifier-backed task failed. The verifier establishes the failure; the
trajectory supplies causal evidence. Require the prepared workspace root, exact failed
outcome fields, and its `outcome_score_id`. Never infer or replace the outcome.

## Investigation

Use at most 20 trace-tool calls:

1. Ground the symptom from verifier evidence: state the expected and actual outcome.
2. Call `get_trace_schema` to skim the trace before reading content.
3. Call `query_spans` with the smallest useful selectors: entity or observation ID, tool,
   span type, error flag, UTC range, or deterministic text filter.
4. Call `get_span_context` only for plausible causal observations. Work backward from the
   terminal mismatch through finalization, state verification, mutations, tool results, and
   the evidence that drove those actions.
5. Select the earliest unrecovered observation whose correction could plausibly change the
   failed outcome. Earlier recovered errors remain evidence but are not the critical point.

Stop searching once the causal claim is supported. Do not fetch every page or load a complete
trace when bounded filters answer the question.

## Classification

For supported evidence, select exactly one type:

- `intent_plan_failure`: the task or constraints were misunderstood or planned incorrectly.
- `tool_interaction_failure`: tool choice, arguments, invocation, or recovery was defective.
- `evidence_grounding_failure`: evidence was invented, stale, omitted, or misinterpreted.
- `control_flow_failure`: execution looped, retried badly, lost state, or finalized early.
- `policy_failure`: an instruction, permission, approval, or safety boundary was violated.

Set `evidence_status=supported` only when the critical observation is among one to ten cited
observation IDs and the trace supports both a root cause and the action that should have
occurred there. A failed tool call alone is not causal if the agent recovered.

If the evidence cannot distinguish a causal explanation, set `evidence_status=inconclusive`,
leave `issue_type`, `critical_observation_id`, `root_cause`, and `counterfactual_action` empty,
and state the missing evidence in `inconclusive_reason`. Never force a category.

## Record

Call `record_failure` exactly once with the prepared worktree as `workspace_root`, the exact
failed outcome receipt, `evidence_status`, expected and actual outcomes, and cited observation
IDs. A supported diagnosis must also pass `issue_type`, `critical_observation_id`,
`root_cause`, and `counterfactual_action`; an inconclusive diagnosis passes only its
`inconclusive_reason` for those conditional fields. Retain the returned
`.workspace/failures/<artifact-id>.json` path. The artifact is compact diagnosis state;
Langfuse remains the source of trace content.

Do not call `record_outcome`, modify Langfuse, copy trace payloads locally, cluster failures,
generate datasets, recommend a harness component, edit the harness, or propose a repair while
following this skill. Those are later workflow phases.
