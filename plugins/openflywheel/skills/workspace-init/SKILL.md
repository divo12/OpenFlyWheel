---
name: workspace-init
description: Initialize an OpenFlywheel ITSM-bench optimization workspace by inspecting an agent-harness repository, collecting its experiment configuration one field at a time, creating the managed PROGRAM.md placeholder, and handing preparation to workspace_prepare. Use when onboarding a primary harness for ITSM-bench; do not use for other benchmarks, an already-prepared workspace, ordinary trace queries, or outcome recording.
---

# Workspace Init

Initialize one primary agent harness for ITSM-bench without starting optimization
prematurely. This skill supports only `benchmark: itsm-bench`. Stop and report the
unsupported benchmark if the user requests anything else.

## 1. Discover the harness

Read the repository before asking questions. Identify likely prompts, tools, agent loops,
benchmark runners, verifiers, observability wiring, and editable harness files. Present the
most likely primary harness and ask the user to confirm it. Do not choose among materially
different harnesses without confirmation.

## 2. Collect the experiment configuration

Ask one focused question at a time. Infer repository facts first and recommend a default
when the evidence supports one. Collect, in order:

1. Harness root and explicitly editable files or directories.
2. Optimization goal, primary metric, target, and stopping condition. Keep quality, cost,
   and latency constraints separate rather than hiding them in one average.
3. ITSM-bench root, Harbor task manifest or selection, and expected task count.
4. Authoritative verifier, reward interpretation, and pass threshold.
5. Frozen model, reasoning effort, concurrency, per-task timeout, retry policy, and budget.
6. Langfuse environment, release, and session naming rule.

Never request secret values in chat or write them into configuration. Check only whether
the required environment-variable names are present.

Summarize the complete proposed experiment and obtain confirmation before writing files or
starting a potentially costly baseline. Then create `<harness-root>/experiment_config.yaml`
using only the confirmed values.

## 3. Create the managed program placeholder

Copy [assets/PROGRAM.md](assets/PROGRAM.md) byte-for-byte to
`<harness-root>/PROGRAM.md`. Do not overwrite an existing different `PROGRAM.md`; stop and
ask whether the existing program should be preserved or replaced.

Do not compose the final program yourself. Call `workspace_prepare` with the confirmed
experiment configuration. That tool owns validation, baseline execution, result parsing,
and deterministic composition from `program_templates/base.md` and
`program_templates/itsm.md`.

`workspace_prepare` is long-running and re-entrant:

- On `running`, retain the preparation ID and poll the same request after the returned
  interval. Never start a second baseline.
- On `failed`, report its typed recovery instruction and stop at its declared stop
  condition.
- On `ready`, retain the baseline artifacts and confirm that `PROGRAM.md` is no longer the
  placeholder.

If `workspace_prepare` is unavailable, stop after the confirmed configuration and
placeholder. Report that workspace preparation is not installed. Do not replace the tool
with an improvised shell command.

## 4. Hand off to the optimization program

When preparation is `ready`, start a fresh Codex session with exactly this task:

```text
Read PROGRAM.md and start the optimization loop.
The baseline is already recorded. Start from step 2 (analyze failures).
```

The generated program is authoritative for editable files, gates, metrics, budgets, and
stopping conditions. Do not rerun the baseline, weaken the verifier, expose held-out traces,
or continue past the declared goal or stop condition.
