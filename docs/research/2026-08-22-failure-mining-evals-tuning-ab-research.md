# Failure mining, agent evals, behavior tuning, and A/B testing

Generated: 2026-08-22
Scope: production agent traces → trusted failures → evals → harness edits → paired comparison → governed promotion
Method: current OFW code audit plus primary papers and official platform documentation

## Executive conclusion

The existing OFW architecture is directionally correct: it keeps the model and verifier fixed, mines revision-attributed production traces, preserves evidence anchors, separates frontier/regression/selection/admission data, edits only declared files, evaluates champion and candidate on the same cases, and stops unattended promotion at a review artifact.

Three gaps are important enough to implement now:

1. **A diagnosed cluster is currently exported before anyone confirms the diagnosis.** Production systems and recent trajectory-attribution work distinguish detection from attribution. OFW should keep `PROPOSED` clusters in review and require a content-bound confirmation before they can enter an eval or sealed holdout.
2. **The paired gate reports point estimates but no uncertainty.** Agent behavior is stochastic. OFW should persist wins, losses, ties, discordant-pair count, and an exact one-sided sign-test probability. A policy may require statistical evidence, while small early suites may explicitly use effect-size-only gating.
3. **The next tuning iteration lacks a compact drill-down index.** Raw results exist, but a proposer has no durable map from failure cluster and source trace to baseline/candidate feedback, prediction error, and raw artifacts. OFW should write this index without replacing or summarizing away the source evidence.

Two tempting additions are deliberately deferred:

- **Implicit dissatisfaction mining** should produce review candidates, not verified failures. User corrections, rephrasing, and abandonment are promising signals, but collecting message content is sensitive and the current OFW collection contract intentionally avoids it.
- **Online production A/B experimentation** needs real deployment ownership, traffic allocation, exposure logging, guardrail metrics, and sufficient sample size. OFW can consume such results later; it should not pretend its offline paired replay is a live randomized experiment.

## 1. What failure mining should mean

### 1.1 Detection, attribution, and remediation are different claims

TRACE mines historical trajectories for corrections, rephrasing, abandonment, and other dissatisfaction cues, then separately performs multi-component attribution and exploratory verification. Its reported results—72.7% root-cause attribution and 82% end-to-end fix effectiveness on 60 dissatisfaction traces—also show that attribution is useful but not infallible. A trace signal therefore cannot automatically authorize a harness edit. [TRACE: TRajectory Attribution for Automated Context Engineering](https://arxiv.org/abs/2608.09153)

AgentEval reaches the same conclusion from a workflow perspective: modeling step dependencies as a DAG materially improves failure detection and root-cause accuracy over flat or end-to-end grading. It reports 0.89 versus 0.41 failure-detection recall and 72% root-cause accuracy against an 81% human ceiling across three production workflows. [AgentEval](https://arxiv.org/abs/2604.23581)

AgentDebugX similarly organizes debugging as Detect → Attribute → Recover → Rerun and emphasizes that the step where an error surfaces may not be the cause. [AgentDebugX](https://arxiv.org/abs/2607.18754)

**OFW decision:** retain strict independent evidence for `VERIFIED_FAILURE`; keep diagnosis as a proposal; add a separate, content-bound cluster-review transition before eval export. Do not infer a verified failure from an error span or user behavior alone.

### 1.2 Full trajectories matter, but summaries should be indexes

Meta-Harness stores each candidate’s source, scores, and execution traces in a filesystem and lets a coding agent retrieve evidence selectively. In its classification ablation, scores-only reached 34.6 median accuracy, scores plus generated summaries reached 34.9, and full trace access reached 50.0. The paper’s inference is that compression can remove the information required to locate an upstream cause. [Meta-Harness](https://arxiv.org/abs/2603.28052)

AHE resolves the scale problem with layered experience observability: a compact corpus supports navigation, while the raw evidence remains available for drill-down. It also adds decision observability, binding an edit’s predicted effects to later task-level outcomes. [Agentic Harness Engineering](https://arxiv.org/abs/2604.25850)

**OFW decision:** create a typed experience index containing developer source trace/snapshot references, paired baseline and candidate verifier feedback, case deltas, and raw developer benchmark-result paths. Selection and admission remain sealed and contribute only their pass/fail decisions. The index is not a lossy replacement for the existing manifests.

### 1.3 Trace shape and privacy are part of mining correctness

OpenTelemetry’s GenAI conventions define agent, model, retrieval, and tool spans, including stable agent/version, conversation, tool-name, call-id, error, and usage attributes. Message content, system instructions, tool arguments, and results are opt-in because they may contain sensitive data. [OpenTelemetry GenAI conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md), [GenAI span conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md)

Langfuse likewise recommends stable trace/observation names, environment separation, meaningful input/output, and evaluation context because downstream datasets and evaluators depend on trace structure. [Langfuse observability best practices](https://langfuse.com/docs/observability/best-practices)

**OFW decision:** do not expand content capture in these PRs. First preserve the existing metadata-only privacy boundary. A later opt-in content policy must define redaction, retention, consent, and which signals may enter review.

## 2. Turning production failures into evals

### 2.1 Production examples need curation and provenance

Both Langfuse and LangSmith support turning selected production traces into dataset items while retaining source trace/observation linkage. LangSmith additionally supports annotation queues in which reviewers correct inputs, outputs, and references before adding them to a dataset. [Langfuse datasets](https://langfuse.com/docs/evaluation/experiments/datasets), [LangSmith dataset management](https://docs.langchain.com/langsmith/manage-datasets-in-application), [LangSmith annotation queues](https://docs.langchain.com/langsmith/annotation-queues)

Langfuse versions dataset membership, and experiments bind dataset items, traces, observations, and scores. That lineage is necessary to explain which production example produced an eval and which version was run. [Langfuse experiment data model](https://langfuse.com/docs/evaluation/experiments/data-model)

**OFW decision:** preserve the existing immutable snapshot and partition ledger. Add review provenance at the cluster boundary rather than copying examples into a second database.

### 2.2 A good agent task validates outcome and process

Anthropic distinguishes the transcript from the outcome: an agent may claim success while the environment state proves otherwise. It recommends multiple graders, multiple trials, reference solutions, balanced positive/negative cases, separate capability and regression suites, transcript inspection, and ongoing task maintenance. It suggests that 20–50 real tasks can be enough to begin when effect sizes are large. [Anthropic, “Demystifying evals for AI agents”](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

OpenAI’s grader APIs similarly separate dataset schema from testing criteria and support deterministic, model-based, Python, and composite graders. OpenAI’s GDPval uses blinded expert pairwise comparisons and detailed rubrics; its automated grader remains an estimate rather than a replacement for experts. [OpenAI Evals API](https://platform.openai.com/docs/api-reference/evals), [OpenAI Graders](https://platform.openai.com/docs/api-reference/graders), [GDPval](https://openai.com/index/gdpval/)

Agent trajectory research adds another warning: outcome-only metrics can hide inefficient or unsound reasoning, while step-level dependencies can expose propagated failures. [Trajectory-Aware Comprehensive Evaluation](https://arxiv.org/abs/2602.21230), [AgentDiagnose](https://aclanthology.org/2025.emnlp-demos.15/)

**OFW decision:** keep the frozen environment/verifier as the outcome authority and retain complete trial records. Cluster confirmation will control dataset admission; a future step-level grader can use the same evidence-anchor model without changing the ledger.

### 2.3 Holdouts must remain selection instruments, not tuning data

Langfuse dataset versions and experiment runs, LangSmith dataset splits, and standard model-selection practice all separate search data from final evaluation. Meta-Harness explicitly deduplicates and decontaminates its held-out math corpus. General model-selection guidance warns that repeated selection on a holdout turns it into training data. [Langfuse datasets](https://langfuse.com/docs/evaluation/experiments/datasets), [LangSmith datasets](https://docs.langchain.com/langsmith/manage-datasets-in-application), [Model evaluation and selection](https://arxiv.org/abs/1811.12808), [Meta-Harness](https://arxiv.org/abs/2603.28052)

**OFW decision:** retain the existing family-level `PartitionLedger`, one-time admission, and candidate-facing holdout isolation. Cluster review must not reveal selection or admission payloads to the proposer.

## 3. Tuning agent behavior without weight training

### 3.1 Textual feedback carries more information than scalar reward

GEPA reflects over trajectories, tool outputs, and domain-specific text feedback, then evolves candidates using Pareto selection. The paper reports a 6% average improvement over GRPO with up to 35× fewer rollouts. DSPy’s official GEPA guidance specifically notes that component-level feedback lets the optimizer identify which aspect needs improvement. [GEPA](https://arxiv.org/abs/2507.19457), [DSPy GEPA documentation](https://dspy.ai/tutorials/gepa_ai_program/)

Meta-Harness goes further for code-level harnesses: its proposer reads prior source and raw traces rather than relying on a fixed critique template. AHE makes the editable component action space explicit and attributes each predicted fix/regression against later results. [Meta-Harness](https://arxiv.org/abs/2603.28052), [AHE](https://arxiv.org/abs/2604.25850)

ACE shows why memory updates should be incremental rather than repeated full rewrites: its generator/reflector/curator design aims to avoid brevity bias and context collapse. [Agentic Context Engineering](https://arxiv.org/abs/2510.04618)

**OFW decision:** do not add a provider-specific proposer to core. Add a durable provider-neutral experience index. Candidate generators can then inspect dense feedback and raw artifacts through ordinary file tools.

### 3.2 Optimize the harness surface, not only the system prompt

AHE’s ablations attribute gains to tools, middleware, and long-term memory rather than the system prompt; its frozen harness also transfers across model families. Meta-Harness searches harness code, while GEPA’s published experiments focus on text/prompt evolution. [AHE](https://arxiv.org/abs/2604.25850), [Meta-Harness](https://arxiv.org/abs/2603.28052), [GEPA](https://arxiv.org/abs/2507.19457)

**OFW decision:** retain file-level editable components and frozen verifier/runtime/model boundaries. The new work will not add a prompt-only optimizer or weight fine-tuning.

### 3.3 Judgment Labs: search, verify, adapt

Judgment Labs presents a production workflow centered on behavior rather than isolated traces: start from a reported incident, search for similar trajectories, quantify recurrence and affected cohorts, narrow a root cause, turn the behavior into an agent test, compare runs, and continuously monitor recurrence. Its public site also exposes the agent through Slack and MCP rather than requiring users to operate a separate dashboard. These are vendor product claims, not independently reproduced results, but the workflow is concrete and maps closely to OFW’s control-plane goal. [Judgment Labs](https://www.judgmentlabs.ai/)

Its Agent Judge article argues that a long-horizon evaluator needs three capabilities: targeted search over queryable trajectories, verification against durable environment state, and adaptation of versioned rubrics from human feedback and judge disagreement. Judgment reports that its refined internal agentic judge improved from 0.76 to 0.86 accuracy over five rubric refinements on an internal production-traffic hallucination dataset. Because the dataset and implementation are not public, OFW treats the numbers as directional vendor evidence rather than a benchmark claim. [Judgment Labs Agent Judge](https://www.judgmentlabs.ai/blogs/agent-judge-solving-long-context-evaluations)

Judgment’s ABM perspective adds four operational stages: capture permissioned production trajectories, bucket recurring behavior/failure modes, mine preferences into small operational rubrics, and only then turn validated scores into rewards. It explicitly warns that generic judges and static rubrics drift away from production behavior. [Judgment Labs, “Climbing the Hills That Matter”](https://www.judgmentlabs.ai/blogs/climbing-the-hills-that-matter)

**OFW decision:** adopt the workflow, not the product surface. Failure clusters remain behavior objects with recurrence and evidence. Agent-generated diagnoses remain proposals. Environment verification will require explicit read-only source-of-truth connectors. Rubric evolution is deferred until OFW has human labels and disagreement data to validate it.

### 3.4 Using Hermes effectively as a mining agent

Hermes provides non-interactive one-shot execution, explicit model/provider selection, toolset restriction, isolated worktrees, skills, subagents, batch trajectory generation, and an Azure Foundry provider. Its own architecture guidance recommends capabilities at the edge and warns against third-party integrations in the core. [Hermes CLI documentation](https://hermes-agent.nousresearch.com/docs/user-guide/cli), [Hermes providers](https://hermes-agent.nousresearch.com/docs/integrations/providers), [Hermes source](https://github.com/NousResearch/hermes-agent)

The installed Chorus environment exposes `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_BASE_URL`, and `AZURE_OPENAI_DEPLOYMENT`; Hermes’s bundled Azure Foundry profile expects the equivalent Foundry key/base URL plus an explicit model. OFW must not read, copy, persist, or log these secret values. The operator may map them in the parent process before launching OFW/Hermes.

The safe integration is a narrow diagnoser adapter:

1. OFW validates registered asset content against the immutable harness revision, then builds an in-memory evidence packet with the trace snapshot and those assets.
2. OFW embeds the snapshot and registered component contents into one bounded evidence prompt and sends it over stdin to an audited Hermes 0.20.0 Python bridge. A disposable `HERMES_HOME`, safe mode, the built-in compressor, and the restricted context-engine toolset leave the model with no filesystem, terminal, browser, web, memory, skill, plugin, or subagent tools.
3. Provider, model, reasoning level, Hermes version, prompt version, timeout, and prompt budget are fingerprinted.
4. Hermes returns one `TraceDiagnosis` JSON proposal. Invalid output, timeout, nonexistent evidence anchors, or component mismatch becomes an abstention.
5. Hermes never confirms a cluster, creates an eval, sees holdout payloads, edits the production repository, or promotes a candidate.

This design gets Judgment-style targeted investigation and AHE-style component inspection without coupling OFW core to Hermes internals. A later batch coordinator can give Hermes a proposer-visible experience index and allow bounded subagents, but only after cost and information-flow controls are proven.

## 4. Paired offline comparison and online A/B testing

### 4.1 Offline replay is paired evaluation, not a production A/B test

OFW currently evaluates champion and candidate on the same case/repeat identity. That pairing is valuable because it removes case-mix variation, but it is still offline replay. Anthropic describes production A/B testing as real-traffic comparison that measures user outcomes and may require days or weeks to reach significance. [Anthropic agent eval guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

**OFW decision:** name the current evidence “paired offline comparison.” Do not claim live causal impact until a deployment adapter supplies randomized exposure and outcome records.

### 4.2 Stochastic agents require repeated trials and uncertainty

Anthropic recommends multiple trials and distinguishes pass@k (at least one success) from pass^k (all trials succeed). Which one matters depends on whether occasional success or reliable consistency is the product requirement. [Anthropic agent eval guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

General model-selection literature recommends uncertainty estimates, bootstrap methods, and multiple-comparison correction when choosing among many candidates. [Raschka, “Model Evaluation, Model Selection, and Algorithm Selection”](https://arxiv.org/abs/1811.12808)

Chatbot Arena demonstrates pairwise estimation with confidence intervals and notes the cost of simultaneous comparisons; its adaptive sampling focuses observations on close competitors. [Chatbot Arena](https://arxiv.org/abs/2403.04132)

**OFW decision:** record an exact paired sign-test probability from discordant champion/candidate outcomes. Keep effect-size thresholds and critical-regression rules primary. Statistical gating is opt-in and must declare a minimum number of discordant pairs; absence of evidence is not evidence of equality.

### 4.3 Model judges require calibration and order control

LangSmith’s comparative evaluation API can randomize answer order to mitigate positional bias. Independent research confirms that position bias varies materially across judges and tasks. Human-anchored calibration remains necessary, especially for subjective rubrics. [LangSmith pairwise evaluation](https://docs.langchain.com/langsmith/evaluate-pairwise), [Judging the Judges](https://arxiv.org/abs/2406.07791)

**OFW decision:** continue preferring deterministic verifiers for hard gates. Model-judge adapters remain possible, but a future adapter must freeze the rubric/model, balance presentation order, and retain calibration evidence.

## 5. Current OFW gap audit

| Area | Current evidence | Gap | Decision |
|---|---|---|---|
| Failure truth | Revision attribution, trusted score sources, ambiguous/conflicting quarantine | Diagnosis `PROPOSED` state does not prevent eval export | PR14: content-bound cluster review and export admission gate |
| Failure attribution | Typed mechanisms, components, source traces, observation/score anchors, lineage | No human/independent confirmation transition | PR14 |
| Dataset leakage | Family-level immutable ledger and one-shot admission | No change required | Preserve |
| Trials | Repeat index and paired attempt identity exist | No uncertainty or discordant-pair report | PR15: exact paired evidence |
| Candidate tuning | File allowlist, manifest, expected effects, full result manifests | No layered next-iteration index of feedback/raw evidence | PR17: experience index |
| Agentic diagnosis | Python diagnoser over one immutable snapshot | No safe external agent adapter for targeted component inspection | PR16: sandboxed Hermes diagnoser |
| Judge quality | Frozen Python/command verifiers | No calibrated model-judge adapter | Defer until a real adapter is requested |
| Implicit dissatisfaction | Metadata/status/scores collected; content minimized | Corrections/rephrasing/abandonment unavailable and sensitive | Defer behind opt-in content policy |
| Online A/B | Offline pairing, PR/deploy adapter, post-monitor job type | No random exposure or production outcome contract | Defer until deployment owner supplies traffic/outcomes |

## 6. Stacked implementation plan

### PR14 — Confirmed failure clusters before eval admission

- Add immutable `ClusterReview` bound to cluster id, revision, content digest, reviewer, decision, and time.
- Add a pure curation operation that emits a new content-addressed `DiagnosisResult` with confirmed/rejected states.
- Route unconfirmed and rejected failure clusters to `REVIEW`; only `CONFIRMED`/`TARGETED` clusters may enter eval, memory, selection, or admission partitions.
- TDD: proposed stays review, valid confirmation exports, stale/forged review fails, rejected stays review, holdout payload remains hidden.

### PR15 — Uncertainty-aware paired comparison

- Add typed `PairedEvidence` per partition: wins, losses, ties, discordant count, net delta, exact one-sided sign-test probability.
- Add explicit `StatisticalGateMode`: effect-size-only or exact-sign-test.
- Require minimum discordant evidence and maximum probability only in exact mode; preserve critical-regression and cost/latency gates.
- TDD: all ties, one-sided wins, symmetric evidence, insufficient evidence, repeated stochastic trials, and policy digest/cache binding.

### PR16 — Sandboxed Hermes diagnosis proposals

- Add a typed `HermesDiagnoser` that invokes a pinned one-shot command through the existing execution boundary.
- Validate and read only connected harness assets, then serialize them with the immutable trace snapshot into an in-memory evidence packet.
- Force an isolated Hermes home and safe mode with an empty effective tool surface; pass the bounded prompt over stdin rather than CLI arguments, validate the final response as `TraceDiagnosis`, and fail closed to abstention.
- Keep credentials inherited and out of manifests/logs; fingerprint command, provider, model, reasoning, Hermes version, timeout, prompt protocol, and prompt budget.
- TDD: component-only visibility, no source mutation, typed proposal, invalid output/timeout abstention, fingerprint drift.

### PR17 — Drill-down optimization experience index

- Write one content-bound experience manifest per Fit campaign.
- Index developer cluster/source trace/snapshot, case partition, paired baseline/candidate verdicts, all developer verifier feedback, prediction error, and raw developer benchmark result paths.
- Validate the index on cached Fit reads and expose a typed reader for provider-specific proposers.
- TDD: feedback preserved byte-for-byte, source trace linkage, rejected/winner histories, artifact tamper rejection, no holdout payload copied into proposer-visible fields.

### PR18 — Research-backed end-to-end release update

- Update the offline trace-to-review fixture to confirm clusters, emit paired evidence, and validate the experience index.
- Re-run full typing, security, package, and coverage gates.

## 7. Rejected over-engineering

- No vector database for clustering or history retrieval; filesystem manifests and exact identifiers are sufficient in local v0.
- No generic online experimentation service; production exposure allocation is not currently owned by OFW.
- No automatic implicit-signal promotion to verified failure.
- No provider-specific optimizer in core.
- No weight tuning.
- No uncalibrated LLM judge as the sole promotion gate.

## Sources

1. [Anthropic: Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
2. [Agentic Harness Engineering](https://arxiv.org/abs/2604.25850)
3. [Meta-Harness](https://arxiv.org/abs/2603.28052)
4. [GEPA](https://arxiv.org/abs/2507.19457)
5. [DSPy GEPA documentation](https://dspy.ai/tutorials/gepa_ai_program/)
6. [Agentic Context Engineering](https://arxiv.org/abs/2510.04618)
7. [TRACE: Trajectory Attribution for Automated Context Engineering](https://arxiv.org/abs/2608.09153)
8. [AgentEval](https://arxiv.org/abs/2604.23581)
9. [AgentDebugX](https://arxiv.org/abs/2607.18754)
10. [AgentDiagnose](https://aclanthology.org/2025.emnlp-demos.15/)
11. [Automated structural testing of LLM-based agents](https://arxiv.org/abs/2601.18827)
12. [Langfuse evaluation overview](https://langfuse.com/docs/evaluation/overview)
13. [Langfuse datasets](https://langfuse.com/docs/evaluation/experiments/datasets)
14. [Langfuse experiment data model](https://langfuse.com/docs/evaluation/experiments/data-model)
15. [LangSmith evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts)
16. [LangSmith dataset management](https://docs.langchain.com/langsmith/manage-datasets-in-application)
17. [LangSmith annotation queues](https://docs.langchain.com/langsmith/annotation-queues)
18. [LangSmith pairwise evaluation](https://docs.langchain.com/langsmith/evaluate-pairwise)
19. [OpenTelemetry GenAI agent conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
20. [OpenAI Evals API](https://platform.openai.com/docs/api-reference/evals)
21. [OpenAI Graders](https://platform.openai.com/docs/api-reference/graders)
22. [OpenAI GDPval grading](https://openai.com/index/gdpval/)
23. [Chatbot Arena](https://arxiv.org/abs/2403.04132)
24. [Judging the Judges](https://arxiv.org/abs/2406.07791)
25. [Model evaluation and selection](https://arxiv.org/abs/1811.12808)
26. [Trajectory-Aware Comprehensive Evaluation](https://arxiv.org/abs/2602.21230)
27. [Judgment Labs](https://www.judgmentlabs.ai/)
28. [Judgment Labs Agent Judge](https://www.judgmentlabs.ai/blogs/agent-judge-solving-long-context-evaluations)
29. [Judgment Labs: Climbing the Hills That Matter](https://www.judgmentlabs.ai/blogs/climbing-the-hills-that-matter)
30. [Hermes CLI documentation](https://hermes-agent.nousresearch.com/docs/user-guide/cli)
31. [Hermes providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)
32. [Hermes Agent source](https://github.com/NousResearch/hermes-agent)

## Methodology and confidence

The research used 16 search queries and deep-read 24 primary papers, official documentation pages, or requested vendor materials. Product-documentation claims are treated as descriptions of product behavior, not independent empirical evidence. Judgment Labs’s internal benchmark is explicitly labeled vendor-reported. Empirical claims from papers are reported with their study scope. The design recommendations are OFW inferences, explicitly labeled as decisions above.

Confidence is high for the three immediate gaps because each is supported by multiple independent sources and directly observable in the current code. Confidence is medium for implicit dissatisfaction mining because the strongest recent evidence uses content that OFW intentionally does not collect. Confidence is low for a generic online A/B implementation without a concrete deployment/traffic owner; it is therefore deferred.
