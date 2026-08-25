---
name: ofw-mine-failures
description: Mines observable failures from executed Hermes trajectories through a connected OpenFlyWheel failure-mining MCP server. Use when asked to judge, triage, or mine failures from OFW/Langfuse traces. Do not use for root-cause diagnosis, clustering, eval generation, rubric refinement, or modifying Hermes.
---

# Mine Hermes failures with OpenFlyWheel

Investigate one OFW mining case and return an evidence-grounded failure-mining
result. Hermes is the executed agent. You are the Codex operator using OFW.
Stay at observable behavior; diagnosis and improvement are separate work.

## Principles

1. **The oracle decides completion.** A Hermes claim is evidence of what it
   claimed, not proof that the task succeeded. Verify every required outcome
   against its declared environment source.
2. **Recovery matters.** A failed action is not a task failure when Hermes later
   recovers and every required outcome is completed.
3. **Use only issued evidence.** Cite observation and environment evidence
   returned by OFW tools. Never invent identifiers, digests, state, or tool
   results.
4. **Read the complete trajectory.** Search to find relevant regions, then page
   through `read_trajectory` until `next_cursor` is null. Keep only relevant
   evidence in the result.
5. **Calibration cannot override state.** `adapt` returns human and production
   signals from nominated trajectories. Use them to challenge an interpretation,
   never to replace source-of-truth verification.
6. **No diagnosis.** Do not identify a root cause, responsible component, bad
   prompt, broken tool, or proposed fix. Do not cluster failures, create evals,
   or change a rubric.

## Workflow

1. Call `get_mining_case`. Read the task intent, constraints, required outcomes,
   available Hermes tools, environment sources, observation IDs, and nominated
   signals.
2. Search the current trajectory with `search_trajectory`:
   - search task-specific entities and required outcomes;
   - search completion claims such as `done`, `success`, or `completed`;
   - search errors, failed actions, retries, cancellations, and verification;
   - follow new questions raised by each useful hit with another focused search.
3. Page through the ordered trajectory with `read_trajectory`, beginning with a
   null cursor and continuing with each returned `next_cursor` until it is null.
   Track whether errors were retried, recovered, abandoned, or contradicted by a
   later action.
4. Use `search_prior_trajectories` only when a similar run, prior disagreement,
   or repeated signal would clarify the current observable behavior. Prior runs
   do not prove the current outcome.
5. For every required outcome, call `verify_environment` with the exact
   `source_id` and `check_id` returned by `get_mining_case`.
6. Call `adapt` for the relevant nominated signal kinds. Compare those signals
   with the trajectory and verification result. Record disagreement as an
   unresolved question; do not silently choose the signal you prefer.
7. Apply the verdict rules and return the result in the output shape below.

## Verdict rules

- `confirmed_failure`: at least one required outcome is `not_completed`, and a
  concrete `FailureBehavior` is grounded in trajectory plus environment
  evidence.
- `no_failure`: every required outcome is `completed`; `failure_behavior` must
  be null, including when an intermediate action failed but recovery succeeded.
- `ambiguous`: completion cannot be established because required environment
  state is unavailable or evidence materially conflicts; `failure_behavior`
  must be null and `unresolved_questions` must explain the uncertainty.
- Never return `confirmed_failure` from a tool error alone.

Use only these observable behavior categories:

- `outcome_mismatch`
- `false_completion`
- `required_action_omitted`
- `forbidden_state_change`
- `unrecovered_action_failure`
- `no_progress_loop`
- `abandoned_before_completion`

## Output

Return one `FailureMiningResult`-shaped object containing:

```text
task
context
verdict
source_ids
completion_checks
failure_behavior | null
trajectory_evidence
environment_evidence
confidence
unresolved_questions
invalid_reason | null
```

For a confirmed failure, each behavior observation must identify its behavior
kind, phase, first and optional last observation IDs, recovery status, and
trajectory evidence. Keep the summary factual and free of causal claims.

## Decision examples

- A command fails, Hermes retries successfully, and the oracle confirms the
  required state: `no_failure`.
- Hermes says the work is complete, but the oracle shows the required state was
  not reached: `confirmed_failure` with `false_completion` or
  `outcome_mismatch`, depending on the observed behavior.
- Hermes appears to stop early, but the environment cannot be queried:
  `ambiguous`, not an inferred failure.
