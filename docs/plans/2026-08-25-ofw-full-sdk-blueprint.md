# OpenFlyWheel full SDK blueprint

Status: proposed  
Date: 2026-08-25  
Base: merge PR #5 into `fresh`, then execute PR6–PR16 in dependency order  
Executed system: any agent harness connected to Langfuse  
Reference integration and test harness: Hermes  
OFW operator/reasoning agent: Codex  
Outsourced subsystem: Langfuse trace instrumentation, ingestion, storage, and trace UI

## 1. Objective

Build OpenFlyWheel as the complete local-first SDK for turning production agent-harness
experience into evidence-backed evaluation and gated harness improvement, while
using Langfuse instead of building an OFW trace collector.

OFW owns:

- immutable harness definitions and revisions;
- production-signal semantics;
- task, context, behavior, and environment-verification contracts;
- Codex-facing query, judge, diagnosis, eval, and optimization workflows;
- failure records, diagnoses, clusters, and living eval suites;
- reproducible workspaces and experiment execution;
- candidate generation, gates, promotion, rollback, and pins;
- durable lineage, audit history, resumability, CLI/MCP surfaces, and Codex skills.

Langfuse owns:

- client instrumentation and framework adapters;
- trace/span ingestion;
- raw trace persistence and trace UI;
- transport-level buffering, batching, and delivery.

OFW imports immutable, attributed Langfuse windows into local SQLite. It does
not proxy model calls, export OTLP, or become another observability backend.

## 1.1 Research basis

This blueprint derives the product shape from NeoSigma's public
[skills repository](https://github.com/neosigmaai/skills), especially
`integrate-sdk` and `import-verifiers`, plus its posts on
[production evals](https://neosigma.ai/blog/the-most-important-eval-isnt-on-a-leaderboard),
[self-improving systems](https://neosigma.ai/blog/self-improving-agentic-systems),
[agent workspaces](https://neosigma.ai/blog/agent-workspaces), and
[model–harness co-design](https://neosigma.ai/blog/investigating-the-optimal-harness-for-a-model).
Auto Harness is intentionally excluded; it is a minimal user-operated example,
not evidence for NeoSigma's product SDK architecture.

## 2. Product boundary

```text
Any production agent harness
  └─ Langfuse SDK/exporter
       └─ complete traces, tool calls, scores, feedback
            └─ OFW Langfuse connector → immutable local snapshot

Codex + OpenFlyWheel plugin
  ├─ Search / Read / Verify / Adapt
  ├─ Mine observable failures
  ├─ Diagnose confirmed failures
  ├─ Build and maintain eval cases
  ├─ Propose bounded harness changes
  └─ Interpret experiment evidence

OFW deterministic services
  ├─ contracts and lineage
  ├─ state store
  ├─ environment oracles and verification
  ├─ workspace reset and execution
  ├─ regression/frontier gates
  ├─ keep/revert/pin
  └─ budgets, stop conditions, and audit log
```

The agent never decides whether its own change ships. Codex proposes and
investigates; typed OFW services reproduce, verify, gate, and record.

## 3. Target SDK experience

```python
project = ofw.Project(
    harness=ofw.Harness(...),
    traces=ofw.LangfuseProject.from_env(environment="production"),
    state_path=Path(".ofw/ofw.sqlite"),
)

revision = project.process()
collection = project.collect(window=window)
mining_run = project.mine(collection, signals=signal_sources)
diagnosis_run = project.diagnose(mining_run.confirmed_failures)
suite_revision = project.build_evals(diagnosis_run)
candidate = project.fit(suite_revision, budget=budget)
decision = project.gate(candidate)
project.pin(decision)  # succeeds only for an admitted candidate
```

The exact facade may change during implementation. The domain objects and
authority boundaries below may not.

## 4. Invariants

1. The connected agent harness is the executed system; Codex is the only reasoning
   agent using OFW. Hermes is the reference integration, never a domain assumption.
2. Langfuse content is read-only. OFW never edits or deletes source traces.
3. Every admitted trace references one immutable harness revision.
4. An executed agent's completion claim is never an oracle.
5. Stateful success requires read-only environment evidence or a durable
   recorded equivalent.
6. Intermediate action failure is not task failure after verified recovery.
7. Mining observes behavior; diagnosis explains cause; eval building reproduces
   the failure; Fit proposes changes. These remain separate stages and types.
8. Agent outputs are proposals until deterministic validation admits them.
9. Eval holdouts and test traces remain invisible to Codex candidate generation.
10. Every state transition is append-only, content-addressed, and attributable
    to input revisions, evidence, agent session, and code commit.
11. No `Any`, untyped metadata dictionaries, dynamic `getattr`/`setattr`, or
    stringly categorical state in public Python contracts.
12. Failed or interrupted work can resume without replaying completed external
    operations.

## 5. Target package layout

```text
src/ofw/
  contracts.py                 existing revision primitives
  harness.py                   existing component registry
  runtime.py                   existing canary/runtime boundary
  project.py                   final public facade
  state/
    store.py                   OFW-owned SQLite state
    migrations/
  observability/langfuse/      existing outsourced-collector connector
  signals.py                   production/human signal normalization
  verification.py              environment/oracle/test contracts
  mining/                      task/context/behavior + Codex judge sessions
  diagnosis/                   cause and improvement-hypothesis evidence
  evals/                       cases, suites, reproduction, lifecycle
  clusters.py                  recurring failure-mode registry
  workspace/                   resettable E2B execution
  experiments.py              champion/candidate executions
  gate.py                      suite and promotion policies
  fit.py                       bounded Codex harness optimizer
  loop.py                      resumable flywheel orchestration
  mcp/                         Codex tool servers over the same services
plugins/openflywheel/          focused Codex skills
```

Do not perform a directory-only refactor. Move code only when a PR introduces
the owning service and preserves public imports.

## 6. Dependency graph

```text
PR5 evidence-backed mining (current)
  ├─ PR6 durable state + production signals ─┐
  └─ PR7 verification SDK + importer ────────┤
                                              ▼
                                   PR8 executable Codex judge runtime
                                              ▼
                                   PR9 failure diagnosis
                                              ▼
                                   PR10 eval case reproduction
                                      ┌───────┴────────┐
                                      ▼                ▼
                         PR11 cluster/eval registry   PR12 workspaces/experiments
                                      └───────┬────────┘
                                              ▼
                                   PR13 gates + suite transitions
                                              ▼
                                   PR14 bounded Fit optimizer
                                              ▼
                                   PR15 resumable flywheel loop
                                              ▼
                                   PR16 SDK/plugin hardening
```

Parallel lanes:

- PR6 and PR7 may run in parallel after PR5 merges; they own disjoint modules.
- PR11 and PR12 may run in parallel after PR10; one owns intelligence state,
  the other owns execution.
- All other steps are serial because they consume contracts from the preceding
  step.

## 7. PR6 — Durable OFW state and production signals

Branch: `codex/ofw-state-signals`  
Depends on: PR5  
Risk: medium; first OFW-owned persistent schema

### Context brief

Langfuse is the source of raw traces and scores, but OFW needs durable state for
its own derived objects. The existing collection SQLite is a source snapshot,
not the flywheel database. Create a separate `.ofw/ofw.sqlite` with explicit
schema versions and append-only lineage.

Normalize feedback and outcomes without building an OFW event collector.
Initial adapters read Langfuse scores and accept typed caller-provided records.

### Contracts

- `OfwStateVersion`
- `SignalId`, `SignalKind`, `SignalSubject`
- `ProductionSignal`
- `SignalSource` protocol
- `LangfuseScoreSignalSource`
- `StateRecordDigest`

Signal kinds initially cover human feedback, user correction, trusted score,
downstream failure, incident, rollback, reopened work, environment mismatch,
and agent error. Do not add generic string event names to the domain layer.

### Work

1. Add OFW state migrations and a version-checking SQLite store.
2. Persist source provenance, trace/revision identity, timestamps, evidence
   references, and canonical digests.
3. Import Langfuse scores without mutating the collection snapshot.
4. Make ingestion idempotent by signal identity and content digest.
5. Add query methods by revision, trace, kind, and time window.
6. Move `FailureSource` toward the shared signal contract without breaking the
   PR5 public API.

### Verification

```bash
uv run pytest tests/test_state.py tests/test_signals.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Test duplicate ingestion, conflicting identity, wrong revision, corrupt digest,
restart, migration mismatch, and preservation of source evidence.

### Exit criteria

The same Langfuse window can be imported repeatedly with one durable signal
record per source fact, and every record resolves to a collected trace and
revision.

### Rollback

Revert the code while retaining the new SQLite file. Never downgrade or delete
derived records automatically; later code may ignore unsupported schema
versions.

## 8. PR7 — Verification SDK and verifier-import skill

Branch: `codex/verification-contracts`  
Depends on: PR5  
Parallel with: PR6  
Risk: high; this defines what “task completed” means

### Context brief

PR5 has `EnvironmentSource`, `RequiredOutcome`, and an `EnvironmentVerifier`
protocol. Consolidate them into the language-agnostic contract previously
chosen by the user:

```text
environment
oracle
verification tests
```

The environment owns reproducible state. The oracle defines the success fact.
Verification tests observe that fact. Test implementation language is an edge
adapter detail, never a domain category.

### Contracts

- `EnvironmentContract`
- `OracleContract`
- `VerificationTest`
- `VerificationPlan`
- `VerificationAttempt`
- `VerificationEvidence`
- `VerificationStatus`
- `VerificationAdapter` protocol

Initial adapters may wrap recorded state and deterministic commands already
supported by `runtime.py`. External APIs, databases, GitHub, and ticket systems
remain caller-provided adapters until a real integration is requested.

### Work

1. Add I/O-free verification domain objects and canonical digests.
2. Adapt PR5 environment verification to the new plan without compatibility
   shims that duplicate truth.
3. Require read-only operation for mining/judging verification.
4. Record observed version/time and freshness for mutable sources.
5. Add `import-ofw-verifiers` to the Codex plugin. It scans existing tests and
   produces typed plans so users do not write schemas manually.
6. Explicitly skip mechanical checks that do not express agent outcomes and
   stateful checks for which no safe oracle adapter exists.

### Verification

```bash
uv run pytest tests/test_verification.py tests/test_runtime.py -q
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/openflywheel
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Test unsupported oracle, wrong environment version, stale state, mutating
adapter rejection, language-independent command tests, and unavailable state.

### Exit criteria

A user repository can be inspected by Codex and represented as an environment,
oracle, and verification-test plan without hand-authoring OFW objects; a known
state yields a reproducible typed verdict.

### Rollback

Keep PR5 verification operational until this PR's migration tests pass. Remove
old contracts only in the same PR after every caller moves.

## 9. PR8 — Executable Codex judge runtime

Branch: `codex/judge-runtime`  
Depends on: PR6, PR7  
Risk: high; first automated Codex-to-OFW control loop

### Context brief

PR5 exposes one live mining case through MCP, but OFW cannot yet run a complete
Codex judge session or accept its result through a tool. Build the production
runtime that turns the existing `ofw-mine-failures` skill into an executable,
auditable workflow.

### Contracts

- `JudgeSessionId`, `JudgeSessionState`
- `JudgeBudget`, `JudgeAttempt`
- `FailureJudge` protocol
- `CodexFailureJudge`
- `MiningSubmission`

### Work

1. Persist a mining session before launching Codex.
2. Serve multiple nominated cases through MCP with stable case IDs.
3. Add `submit_failure_result`; validate the typed PR5 result and tool-issued
   evidence before admission.
4. Add a Codex CLI/SDK adapter with explicit timeout, token/tool-call budget,
   working directory, plugin/skill selection, and captured session ID.
5. Resume sessions from OFW state after process interruption.
6. Fail closed when Codex returns text without a valid submission.
7. Keep mining read-only: no repository-edit or agent-execution tools.

### Verification

```bash
uv run pytest tests/test_judge_runtime.py tests/test_mcp.py tests/test_mine.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Run an integration smoke over the six existing heterogeneous traces. Require
admitted results for two known successes and four known failures; record
precision errors instead of weakening validators.

### Exit criteria

`project.mine(...)` can launch Codex, expose bounded evidence tools, accept one
typed result per case, persist the session, and resume safely.

### Rollback

Disable the Codex adapter and retain manual/in-process `FailureJudge` support.
Persisted sessions remain inspectable but cannot auto-resume on unsupported
runtime versions.

## 10. PR9 — Failure diagnosis

Branch: `codex/failure-diagnosis`  
Depends on: PR8  
Risk: high; must separate evidence from speculation

### Context brief

Mining answers whether and where the executed agent failed. Diagnosis is the next stage and
may inspect the connected harness repository. It must not mutate files or generate
evals.

### Contracts

- `DiagnosisId`
- `FailureMechanism`
- `FailureLocation`
- `CausalEvidence`
- `CounterfactualCheck`
- `ImprovementHypothesis`
- `FailureDiagnosis`
- `DiagnosisVerdict` including abstention

### Work

1. Add read-only tools for mined evidence, harness revision assets, repository
   search/read, and verifier evidence.
2. Add `ofw-diagnose-failures` Codex skill with explicit no-edit boundary.
3. Require causal claims to cite both failure evidence and relevant harness
   evidence.
4. Distinguish confirmed mechanism, plausible hypothesis, and unknown.
5. Allow multiple hypotheses; never force one root cause.
6. Persist diagnoses with judge session and model/harness revision provenance.

### Verification

```bash
uv run pytest tests/test_diagnosis.py tests/test_diagnosis_mcp.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Test recovered errors, missing source code, multiple plausible mechanisms,
stale harness revision, contradictory evidence, and mandatory abstention.

### Exit criteria

Every confirmed failure has zero or more versioned diagnoses; no diagnosis can
exist without a mined failure and cited evidence.

### Rollback

Diagnosis is additive. Disable the workflow without changing mining results.

## 11. PR10 — Eval case construction and reproduction

Branch: `codex/eval-cases`  
Depends on: PR7, PR9  
Risk: high; generated evals must reproduce real failures

### Context brief

Convert diagnosed production failures into reproducible cases. The core case
contract is:

```text
task
context seed
environment
oracle
verification tests
source failure and diagnosis
```

An agent proposes a case; a reproduction run admits it.

### Contracts

- `EvalCaseId`, `EvalCaseRevision`
- `ContextSeed`
- `EvalCaseCandidate`
- `EvalCase`
- `ReproductionAttempt`
- `ReproductionVerdict`

### Work

1. Add content-addressed eval candidates and immutable admitted revisions.
2. Add `ofw-build-evals` skill and `submit_eval_candidate` tool.
3. Reconstruct only information available before the executed agent begins the task.
4. Bind the case to PR7 environment/oracle/tests.
5. Run the frozen failing revision and require the expected failure behavior or
   failed oracle to reproduce.
6. Reject leakage from final answers, hidden test outputs, and held-out traces.
7. Support deterministic and human-approved admission; an LLM proposal cannot
   admit itself.

### Verification

```bash
uv run pytest tests/test_evals.py tests/test_reproduction.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Test non-reproduction, flaky reproduction, leaked outcome, wrong environment,
missing oracle, source lineage, canonical ID, and duplicate candidates.

### Exit criteria

At least one failure from each supported corpus type can become a reproducible,
immutable eval case without embedding the hidden answer in agent-visible input.

### Rollback

Candidate records remain; mark unsupported candidates rejected. Never delete
admitted eval history.

## 12. PR11 — Failure clusters and living eval registry

Branch: `codex/failure-registry`  
Depends on: PR6, PR9, PR10  
Parallel with: PR12  
Risk: medium

### Context brief

Maintain compact coverage over recurring failure mechanisms rather than one
eval per incident. Start with typed signatures and Codex proposals; do not add a
vector database until retrieval quality measurements justify it.

### Contracts

- `FailureClusterId`, `FailureClusterRevision`
- `FailureSignature`
- `ClusterMembershipProposal`
- `ClusterStatus`
- `EvalLifecycle`: candidate, frontier, regression, retired
- `EvalSuiteRevision`
- `VerifierRevision`
- `CalibrationCase`
- `VerifierCalibration`

### Work

1. Derive a stable signature from behavior, diagnosed mechanism, phase,
   recovery, and required-outcome IDs.
2. Add exact/structured candidate retrieval before semantic similarity.
3. Let Codex propose cluster merge/split/membership with cited examples.
4. Validate proposals against immutable failure and diagnosis records.
5. Track occurrence, severity, resolution attempts, reproduction rate, and eval
   coverage.
6. Add append-only suite transitions and reasons.
7. Compare trace verifiers against human labels, deterministic outcomes,
   environment checks, judge disagreements, and different-outcome runs.
8. Let Codex propose one focused verifier/rubric revision at a time. Admit it
   only on a sealed calibration set; it may not weaken or replace an environment
   oracle.

### Verification

```bash
uv run pytest tests/test_clusters.py tests/test_eval_registry.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Test accidental merge, duplicate failure, split lineage, cluster reopening,
frontier-to-regression transition, retention of critical rare cases, verifier
false-positive/false-negative changes, and rejected rubric goalpost movement.

### Exit criteria

The six-session corpus produces a reviewable set of clusters with explicit
membership evidence and at least one admitted eval for every covered cluster.

### Rollback

Revert cluster algorithms while preserving proposals and previous registry
revisions. Active suite selection points to the last admitted revision.

## 13. PR12 — Reproducible workspaces and experiment execution

Branch: `codex/workspace-experiments`  
Depends on: PR7, PR10, existing PR4 runtime  
Parallel with: PR11  
Risk: high; executes untrusted agent actions

### Context brief

PR4 can run a canary in E2B. Generalize that narrow path into resettable eval
workspaces without building a NeoSigma-style fleet control plane. E2B remains
the first provider; the protocol must permit another provider later.

### Contracts

- `WorkspaceId`, `WorkspaceSnapshotId`
- `WorkspaceSpecification`
- `ExperimentRunId`
- `ExperimentAttempt`
- `RunArtifact`

### Work

1. Provision E2B from a versioned environment specification. Do not introduce
   a provider protocol until a second workspace backend is actually required.
2. Materialize the exact harness revision and eval case.
3. Run setup, health checks, the connected agent harness, and verification with independent limits.
4. Reset filesystem and state between attempts; prove isolation.
5. Keep secrets outside persisted artifacts and agent-visible logs.
6. Collect the resulting trace through Langfuse; do not implement an OFW trace
   exporter.
7. Persist commands, exit state, verifier evidence, trace ID, cost, and timing.

### Verification

```bash
uv run pytest tests/test_workspace.py tests/test_experiments.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Use a small E2B client fake in CI and one opt-in live integration test. Test timeout,
workspace loss, dirty reset, service-not-ready, trace delay, verifier failure,
and secret absence.

### Exit criteria

Champion and candidate revisions can run the same eval from identical initial
state with independently verified outcomes and Langfuse trace IDs.

### Rollback

Keep PR4 canary API working. Disable multi-attempt experiments if the provider
cannot guarantee reset; never reuse uncertain state.

## 14. PR13 — Gates and eval-suite transitions

Branch: `codex/gates-suites`  
Depends on: PR11, PR12  
Risk: critical; only this layer may admit improvement

### Context brief

Implement the deterministic acceptance boundary before building an optimizer.
Codex may interpret failures but cannot waive gates or see held-out evidence.

### Contracts

- `ChampionRevision`
- `CandidateRevision`
- `GatePolicy`
- `GateAttempt`
- `GateReason`
- `PromotionDecision`
- `Pin`

### Work

1. Run the regression suite first and stop on critical-case regression.
2. Run a sealed frontier/validation slice regardless of Codex hypotheses.
3. Compare outcome, critical failures, latency, and cost under explicit policy.
4. Require no unapproved harness files changed.
5. Promote newly resolved frontier cases only after repeated verified success.
6. Record keep/revert and pin decisions append-only.
7. Never delete a prior champion or suite revision.

### Verification

```bash
uv run pytest tests/test_gate.py tests/test_promotion.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Test better average with critical regression, flaky improvement, missing result,
cost dominance, unauthorized edit, holdout leakage, promotion, and rollback.

### Exit criteria

A manually supplied candidate can be admitted or rejected without any Codex
judgment in the acceptance path, and the decision is reproducible from stored
evidence.

### Rollback

Pin the last champion before rollout. Reverting code leaves that pin active;
newer unsupported decisions become read-only history.

## 15. PR14 — Bounded Fit optimizer

Branch: `codex/fit-optimizer`  
Depends on: PR9, PR12, PR13  
Risk: critical; grants Codex repository-write authority

### Context brief

Now allow Codex to propose one focused connected-harness change at a time. The
editable component surface comes from immutable OFW revision metadata. Codex
does not edit OFW infrastructure, verifiers, gates, holdouts, or budgets.

### Contracts

- `FitCampaignId`, `FitBudget`
- `ChangeHypothesis`
- `HarnessChangeProposal`
- `CandidateBuild`
- `FitAttempt`
- `FitOutcome`

### Work

1. Expose only revision assets marked editable.
2. Add `ofw-evolve-harness` skill: inspect cluster/diagnosis/eval evidence,
   propose one change, state expected fixes and at-risk regressions.
3. Validate and apply patches in an isolated git worktree.
4. Process a new immutable harness revision.
5. Execute PR13 gates and keep/revert automatically from the typed decision.
6. Store rejected attempts and their evidence so the next attempt does not
   repeat a failed hypothesis blindly.
7. Enforce iteration, token, wall-clock, experiment, and spend limits.

### Verification

```bash
uv run pytest tests/test_fit.py tests/test_candidate.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Test out-of-surface edit, verifier edit, multiple simultaneous hypotheses,
invalid patch, candidate build failure, gate rejection, accepted change,
budget exhaustion, and resume.

### Exit criteria

Starting from a seeded failing reference harness, Codex can produce a candidate,
OFW can gate it, and only an admitted revision becomes the new champion.

### Rollback

Candidates live in isolated worktrees until admission. Rejected worktrees may
be archived, then deleted; champion branches and pins are never reset
destructively.

## 16. PR15 — Resumable flywheel orchestration

Branch: `codex/flywheel-loop`  
Depends on: PR14  
Risk: high; composes every stage

### Context brief

Compose existing services; do not duplicate their logic in a new “god loop.”
Every transition reads and writes durable state before external work.

### Contracts

- `FlywheelRunId`, `FlywheelStage`
- `FlywheelPolicy`
- `StageAttempt`
- `StopReason`
- `FlywheelReport`

### Work

1. Implement the stage machine:
   collect → signal → mine → diagnose → build/reproduce evals → cluster → fit →
   gate → pin.
2. Make each stage idempotent and resumable.
3. Add stage-specific budgets and global stop conditions.
4. Support human approval gates without requiring them by default for safe,
   local experiments.
5. Emit compact progress reports with artifact IDs, not copied trace content.
6. Add CLI and Python facade entry points over the same service.

### Verification

```bash
uv run pytest tests/test_loop.py tests/test_e2e_release.py -q
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Test crash/restart at every boundary, duplicate external response, unavailable
Langfuse, Codex timeout, no failures, no diagnosis, no reproducible eval,
budget exhaustion, rejected candidate, and admitted candidate.

### Exit criteria

An end-to-end fixture resumes after deliberate process termination and reaches
the same final pin without duplicate cases, experiments, or decisions.

### Rollback

Disable automatic progression and retain manual stage APIs. Durable state shows
the exact last completed stage and safe resume point.

## 17. PR16 — End-to-end SDK facade and Codex plugin release

Branch: `codex/sdk-hardening`  
Depends on: PR15  
Risk: medium

### Context brief

Assemble the already-public stage APIs into the final end-to-end experience.
Every preceding PR must harden its own public imports and must not defer basic
SDK quality to this step. Do not design the facade in advance of service evidence.

### Work

1. Add the `Project` facade over existing services without hiding typed results.
2. Freeze public import paths and add compatibility tests.
3. Complete the OpenFlyWheel Codex plugin with focused skills:
   `integrate-ofw`, `ofw-mine-failures`, `import-ofw-verifiers`,
   `ofw-diagnose-failures`, `ofw-build-evals`, and `ofw-evolve-harness`.
4. Package the supported MCP servers and declare tool dependencies.
5. Add documented generic integration guidance plus a Hermes reference fixture
   using Langfuse and E2B.
6. Add security/privacy documentation, threat boundaries, and secret scanning.
7. Publish migration notes from PR5 APIs and explicit non-goals.

### Verification

```bash
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugins/openflywheel
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
uv build
```

Run a clean-environment install, plugin discovery test, typed example, and one
opt-in live Langfuse/E2B smoke.

### Exit criteria

A fresh Codex session can install the plugin, integrate an existing Langfuse-connected harness
repository without manual OFW schema authoring, run one bounded flywheel cycle,
and inspect every decision artifact.

### Rollback

Keep lower-level service APIs supported for one release. Revert facade/plugin
packaging without changing stored domain records.

## 18. Cross-PR quality gates

Every PR must satisfy:

- tests written before non-trivial implementation;
- full test suite, Ruff, strict mypy, and `git diff --check` clean;
- no secrets, raw credentials, or production trace content committed;
- public contracts use typed classes/enums, never `Any` or metadata bags;
- one migration test for every persisted schema change;
- public imports and typed examples updated in the same PR that adds a service;
- one invalid/adversarial fixture for every admission boundary;
- compatibility maintained or removed explicitly in the same PR;
- PR body states authority gained by Codex and the deterministic control that
  bounds it.

## 19. Success metrics

System quality:

- mining precision/recall against reviewed production failures;
- environment-verification availability and stale-evidence rate;
- diagnosis agreement/abstention against reviewed cases;
- eval reproduction rate and flake rate;
- percentage of recurring failure clusters covered by admitted evals;
- regression-suite escape rate;
- candidate acceptance rate and improvement on sealed validation;
- cost and elapsed time per admitted harness improvement.

SDK quality:

- integration time for a new agent-harness repository;
- fraction of components discovered without user schema authoring;
- restart success at every stage boundary;
- zero duplicate derived records after retry;
- stable public API and plugin activation accuracy.

## 20. Explicit non-goals

- no OFW trace SDK, OTLP collector, trace database, or trace UI;
- no model gateway or proxy;
- no general-purpose production scheduler or warm sandbox fleet in this plan;
- no model weight training or fine-tuning;
- no generic agent framework replacing the connected harness;
- no live production mutation by mining or diagnosis tools;
- no semantic vector database before structured retrieval is measured and found
  insufficient;
- no single `ofw.improve()` tool that collapses proposal and admission.

## 21. Anti-pattern catalog

- **Trace collector creep:** adding spans/exporters because a Langfuse field is
  inconvenient. Extend the connector or require attribution instead.
- **Judge as oracle:** accepting Codex or executed-agent prose instead of environment
  evidence.
- **Stage collapse:** mining records root cause, diagnosis generates evals, or
  Fit edits before a gate exists.
- **Self-admission:** the agent that proposes an artifact also activates it.
- **Holdout leakage:** exposing test traces, expected outputs, or verifier
  internals to candidate generation.
- **Averages hide harm:** accepting better mean reward with critical regression.
- **Mutable history:** updating a failure, cluster, suite, or pin in place.
- **Provider types in domain:** E2B, Langfuse, GitHub, or pytest classes leaking
  into provider-agnostic contracts.
- **Premature orchestration:** building PR15 before individual stages are
  executable and restart-safe.
- **Skill as implementation:** placing business rules only in Codex prose rather
  than typed OFW validators.

## 22. Plan mutation protocol

When implementation evidence invalidates a step:

1. Record the new fact in this plan under a dated `Plan amendments` section.
2. Do not silently broaden the active PR.
3. Split a PR when it gains a second independently releasable authority boundary
   or exceeds one migration plus one service.
4. Insert a prerequisite PR when a required contract or deterministic validator
   is missing.
5. Reorder only when dependency edges and owned files remain valid.
6. Skip a PR only when its exit criteria are already proven by committed tests;
   link the evidence.
7. Abandon a direction after three evidence-backed failures of the same
   assumption, preserving attempts and the selected alternative.

## 23. First move

Merge PR5. Then start PR6 and PR7 in parallel. Do not start diagnosis, eval
generation, clustering, or Fit until the durable signal and verification
contracts are merged.

## 24. Adversarial review decisions

Review completed 2026-08-25.

Accepted:

- Keep E2B as the only workspace backend; remove the speculative provider
  abstraction until a second implementation exists.
- Harden public SDK imports incrementally rather than postponing them to the
  final PR.
- Keep the final flywheel orchestrator thin and dependent on independently
  executable, restart-safe stages.
- Add explicit verifier/rubric calibration to the living eval registry so OFW
  covers the evaluation-maintenance capability, not only case clustering.

Not accepted:

- Merging workspaces, gates, and Fit into one PR. Gate construction must land
  and be testable before Codex receives repository-write authority; combining
  them would let proposal and admission arrive in the same change.
- Collapsing the remaining roadmap to eight PRs. The current eleven steps stay
  within Blueprint's normal range and each high-risk authority boundary remains
  independently reviewable and reversible.

## 25. Plan amendments

None yet.
