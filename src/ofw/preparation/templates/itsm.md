## ITSM-bench Instructions

This program supports only `benchmark: itsm-bench`.

## Record the prepared baseline outcomes

Before diagnosing failures, process every terminal Harbor trial from the prepared baseline:

1. Read the trial's `result.json`, `exception_info`, verifier status, and verifier artifacts.
2. Map a reward only when the verifier completed and the trial has no execution or verifier
   error:
   - Exact `1.0` -> `pass` with score `1.0`.
   - Exact `0.0` -> `fail` with score `0.0`.
   - Any other present reward -> record nothing and report an unsupported-reward mapping
     blocker.
   - An explicit authoritative `abstain` or `error` verdict -> preserve that verdict without
     a score.
   - An exception, verifier error, or missing verifier result without an explicit authoritative
     verdict -> record nothing and report the trial as unverified.
3. Use the task directory name as `task_id`, `itsm-bench@<task_checksum>` as
   `verifier_id`, and the verifier completion time as `evaluated_at`.
4. Resolve exactly one Langfuse trace using the prepared session, environment, release,
   and the trial's agent-execution time window.
5. Call `record_outcome` with stable verifier evidence references and retain its score
   receipt.

If the verifier result is absent, record nothing. If trace selection is empty or ambiguous,
record nothing and report the mapping blocker. `record_outcome` is the only permitted
Langfuse write.

## Analyze failed ITSM trajectories

Use the trace tools in the smallest sufficient sequence:

1. `list_traces` selects candidate traces for the prepared session.
2. `get_trace_schema` skims structure without loading input or output.
3. `query_spans` selects exact observations by ID, tool, type, UTC range, error flag, or
   deterministic text filter.
4. `get_span_context` retrieves bounded raw context only for a selected span.

An intermediate tool error is evidence, not an outcome failure, when the agent recovered
and the verifier passed. A technically clean trajectory is still a failure when the ITSM
verifier shows that the required environment state was not achieved.

## ITSM optimization constraints

- Treat the ITSM verifier score as the authoritative quality metric.
- Read cost and latency from Langfuse; do not write separate cost or latency scores.
- Preserve least-privilege behavior and verify environment state before declaring success.
- Do not expose held-out ITSM tasks or verifier internals to the harness being optimized.
- Run trials sequentially when deterministic trace-to-trial mapping depends on execution
  windows.

## ITSM iteration report

For every candidate run, report verifier passes, verifier failures, unverified trials,
outcome receipts, trace-mapping blockers, the count and values of unsupported-reward mapping
blockers, total Langfuse cost, latency, and the gate decision.
