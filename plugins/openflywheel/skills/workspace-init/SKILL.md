---
name: workspace-init
description: Initialize an OpenFlywheel ITSM-bench optimization workspace by inspecting an agent-harness repository, collecting its experiment configuration one field at a time, and handing isolated branch, worktree, PROGRAM.md, and baseline creation to prepare_workspace. Use when onboarding a primary harness for ITSM-bench; do not use for other benchmarks, an already-prepared workspace, ordinary trace queries, or outcome recording.
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

1. Harness root, Git base ref, sibling worktree parent, and explicitly editable files or
   directories.
2. Optimization goal, primary metric, target, and stopping condition. Keep quality, cost,
   and latency constraints separate rather than hiding them in one average.
3. ITSM-bench root, Harbor executable, Harbor configuration, and expected task count. If an
   exact terminal Harbor job already exists at `benchmark_root/jobs/<experiment-id>`, ask whether
   to adopt it; set `reuse_existing_baseline=true` only after explicit confirmation so policy is
   published without launching a duplicate baseline.
4. Experiment ID and maximum baseline duration.

Read and report the frozen model from the Harbor configuration. `prepare_workspace` fixes
concurrency to one and retries to zero for deterministic trace mapping, uses ITSM-bench as
the authoritative verifier, fixes the Langfuse environment to `itsm-bench`, derives the
session from the experiment ID, and derives the release from the initialization commit. Do
not ask the user to restate those derived values.

Never request secret values in chat or write them into configuration. Check only whether
the required environment-variable names are present.

Summarize the complete proposed experiment and obtain confirmation before writing files or
starting a potentially costly baseline. Pass only those confirmed values to
`prepare_workspace`.

## 3. Prepare the isolated workspace

Do not modify or switch the user's original checkout. Call `prepare_workspace` with the
confirmed experiment configuration. That tool owns validation, creation of an isolated
`ofw/<experiment-id>` branch and sibling Git worktree, deterministic composition of
`PROGRAM.md` from `program_templates/base.md` and `program_templates/itsm.md`, creation of
`experiment_config.yaml`, the initialization commit, baseline execution, and result parsing.

`prepare_workspace` is long-running and re-entrant:

- On `running`, retain the preparation ID and poll the same request after the returned
  interval. Never start a second baseline.
- On `failed`, report its typed recovery instruction and stop at its declared stop
  condition.
- On `ready`, retain the baseline artifacts and confirm that `PROGRAM.md` is no longer the
  placeholder. Use the returned worktree path for every later Codex action.

If `prepare_workspace` is unavailable, stop after confirming the configuration. Report that
workspace preparation is not installed. Do not replace the tool with an improvised shell
command.

## 4. Hand off to the optimization program

When preparation is `ready`, start a fresh Codex session in the returned worktree with
exactly this task:

```text
Read PROGRAM.md and start the optimization loop.
The baseline is already recorded. Start from step 2 (analyze failures).
```

The generated program is authoritative for editable files, gates, metrics, budgets, and
stopping conditions. Do not rerun the baseline, weaken the verifier, expose held-out traces,
or continue past the declared goal or stop condition.
