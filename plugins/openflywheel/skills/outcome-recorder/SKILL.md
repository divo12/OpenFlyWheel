---
name: outcome-recorder
description: Record a completed authoritative verifier result on its exact Langfuse trace. Use after an external verifier returns a final outcome with trace, task, verifier, UTC timestamp, score, and evidence; do not use to infer outcomes or classify implicit failures.
---

# Outcome Recorder

After an authoritative verifier finishes, call `record_outcome` before continuing to failure mining or harness optimization.

- Use the exact `trace_id` emitted for the task run. Never select a trace by guesswork; use the read tools to resolve it when necessary.
- Use a stable `task_id` and versioned `verifier_id`. Use the verifier completion time as UTC `evaluated_at`.
- Map the verifier result directly: `pass` and `fail` require a normalized score from 0 to 1; `abstain` and `error` require no score.
- Include one to ten stable evidence references such as verifier reports, audit artifacts, or environment checks. Do not copy trace blobs or credentials into evidence.
- Treat the returned `score_id` as the receipt. Retrying the same outcome is safe and must return the same logical score.
- Stop and report the missing field when the verifier result cannot satisfy the contract. Never invent a verdict, score, timestamp, evidence reference, or verifier version.

This skill records only authoritative outcomes. It does not judge trajectory quality, mine failure types, promote dataset cases, or alter trace data.
