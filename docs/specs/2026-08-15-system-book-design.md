# OpenFlyWheel System Book Design

Status: Approved design  
Date: 2026-08-15  
First dogfood workspace: Arceus-Inc  
Product scope: Generic company/system onboarding and System Book construction

## 1. Objective

OpenFlyWheel onboards a company or bounded production system, turns heterogeneous evidence into an auditable System Book, and supplies relevant, permission-filtered context to the agents the company already uses.

The System Book is not a search index, a wiki, or a dump of extracted triples. It is a versioned set of load-bearing claims about how a running system currently works. Every active claim has evidence, authority, freshness, and verification state.

The first dogfood workspace covers the Arceus stack, but no Arceus repository, name, ontology, KPI, or topology is hardcoded into the product.

## 2. Research Basis

The design combines two complementary systems:

### Pavo: the Book schema and truth discipline

- Locate and lock one system boundary before broad extraction.
- Organize knowledge under seven Essential Truths:
  - U1: purpose and outcome
  - U2: evaluation, metrics, and guardrails
  - U3: current architecture
  - U4: data flows
  - U5: what has been tried
  - U6: performance and gaps
  - U7: operations and ownership
- Treat `What` and `How` as gold. Treat `Why` as an unverified probe.
- Include only current, production-reachable behavior.
- Use deterministic System-as-Oracle extraction where possible.
- Keep machine extraction concentrated on U3/U4 and ask experts targeted questions for U2/U5/U6/U7.
- Preserve the rule that a claim can be artifact-correct and still system-wrong.

### Hyper: the write path and agent surfaces

- Connect to work already happening instead of requiring a wiki first.
- Filter before storage; do not mirror every source item.
- Preserve admitted raw items as immutable episodes.
- Extract structured facts with provenance and temporal metadata.
- Reconcile new facts with their neighborhood using `derived_from`, `in_tension_with`, and `supersedes`.
- Never erase history when a fact becomes stale.
- Treat agents as bidirectional surfaces: inject context into prompts and ingest useful session evidence.
- Use lifecycle hooks where the platform supports them and MCP everywhere else.

OpenFlyWheel uses Hyper's write path inside Pavo's evidence and verification schema.

## 3. Non-Goals for v0

- No harness-improvement tournament.
- No autonomous KPI optimization.
- No Glean-style enterprise search product.
- No Graphiti or Neo4j dependency.
- No Slack, Gmail, or company-wide firehose.
- No LLM-generated claim may become active without a proposal and verification transition.
- No claim-count KPI that can be inflated by extracting more U3/U4 details.
- No agent-specific memory database.

## 4. Product Boundary

OpenFlyWheel is an independent Python 3.11+ application and library.

It does not import consumer repositories. Repositories, agents, and SaaS tools are sources accessed through adapters. Core contracts remain I/O-free. CLI, MCP, hooks, dashboard, connectors, and storage all call the same application services.

The first workspace is Arceus-Inc. Its local manifest may refer to Arceus, dream, chorus, horizon, and lattice. Those names must never appear in reusable core code or generic tests.

## 5. Onboarding

`ofw onboard` is a staged, resumable workflow.

### 5.1 Workspace

Collect:

- workspace/company name;
- local or shared deployment mode;
- initial administrators and identities;
- default visibility and retention policy.

### 5.2 Connect

Wave 0 has two primary surfaces:

1. **GitHub**
   - organizations and repositories;
   - source, configuration, issues, pull requests, commits, and discussions;
   - collection inventory, timestamps, identities, and permission metadata.
2. **Agents**
   - Claude and Claude Code;
   - Cursor;
   - Codex;
   - OpenClaw;
   - MCP for any other compatible agent.

Expert notes are available as a first-class episode type from day one, but are not presented as a headline connector.

Each connector reports:

- available collections;
- historical-bootstrap capability;
- incremental update mechanism;
- event and ingest timestamps;
- identity and permission fidelity;
- stable source identifiers;
- freshness and deletion semantics.

### 5.3 Locate

Deterministic scans propose candidate:

- system boundaries;
- components and repositories;
- dependency and execution boundaries;
- system shapes;
- owners;
- KPIs and guardrails already visible in evidence.

An LLM may summarize or rank candidates, but cannot lock them.

### 5.4 Lock

A human confirms:

- one system boundary;
- purpose and system shape;
- owners;
- one first KPI;
- source authority rules;
- exclusions and sensitive areas.

The result is a versioned `WorkspaceManifest`. Claim extraction does not begin before this lock.

### 5.5 Bootstrap

- Ingest approved source ranges.
- Construct the initial U1-U7 coverage map.
- Run deterministic extraction before model-assisted extraction.
- Generate targeted expert questions for uncovered or weak U2/U5/U6/U7 requirements.
- Stop with explicit gaps rather than fabricated coverage.

## 6. Processing Pipeline

```text
source item
  -> connector envelope
  -> admission policy
  -> immutable Episode
  -> deterministic SaO and/or grounded LLM extraction
  -> ClaimProposal with EvidenceAnchor records
  -> deduplication and contradiction analysis
  -> authority/human verification
  -> active System Book Claim
```

### 6.1 Admission

Admission is connector-specific and fail-closed.

- Reject junk, unsupported content, secrets, and excluded paths before content persistence.
- Retain only an audit record for rejected items.
- Use source ID plus content hash for idempotency.
- Treat all source content, including agent transcripts, as untrusted evidence rather than instructions.

### 6.2 Episodes

Episodes are immutable admitted source items.

- Corrections create new episodes.
- Summaries never replace source text.
- Every episode carries source identity, ACL, event time, ingest time, checksum, and source reference.
- Connector checkpoints advance only after the episode transaction succeeds.

### 6.3 Extractors

**System-as-Oracle (SaO)** reads machine-verifiable structures such as:

- constants and configuration;
- package and dependency declarations;
- schemas and migrations;
- guards, branches, filters, and state machines;
- tests and executable invariants;
- metric definitions when available.

**Grounded LLM extraction** may process prose, issues, pull requests, discussions, and agent sessions. It must:

- emit atomic claims;
- cite exact episode spans;
- use the locked system shape as a prior, not as evidence;
- abstain when evidence is insufficient;
- produce proposals only.

**Expert extraction** turns an expert answer into a high-authority episode and then into a proposal. Experts do not mutate claim rows directly.

### 6.4 Reconciliation

New proposals are compared with the active neighborhood.

- `derived_from` records evidential or inferential lineage.
- `in_tension_with` preserves unresolved conflict.
- `supersedes` closes an old validity interval without deleting history.
- Recency alone never determines truth.
- Authority, verification state, production reachability, event time, and evidence agreement determine promotion.

## 7. Core Contracts

### Workspace

Tenant-level identity, policy, deployment mode, and administrators.

### SystemBoundary

The locked system, its purpose, shape, owners, KPI, source scope, exclusions, and manifest version.

### Source

A configured GitHub, agent, file, or future SaaS surface and its capability/permission metadata.

### Episode

An immutable admitted source item with content, temporal fields, ACL, checksum, and source reference.

### EvidenceAnchor

An exact, re-checkable location such as repository SHA plus file/line range, issue comment, transcript message, document span, SQL definition, or dashboard field.

### ClaimProposal

An untrusted candidate carrying atomic `what`, evidence anchors, proposed truth section, optional SPO representation, confidence inputs, and proposer identity.

### Claim

An accepted `What + How` filed under U1-U7 with state, authority, temporal intervals, ACL, and edges. `Why` is stored separately as a probe and never presented as gold.

### CoverageRequirement

A shape-specific required knowledge slot with verification and evidence requirements.

### Pin

An immutable snapshot manifest that makes historical reads reproducible.

## 8. Coverage KPI

The primary v0 KPI is verified coverage across the seven Essential Truths.

```text
section coverage =
  verified, evidence-backed required slots
  / total required slots

overall coverage =
  macro-average(section coverage for U1 through U7)
```

Raw claim count does not affect coverage. Each section is reported separately. A large number of architecture claims cannot hide an empty operations section.

The initial requirement set comes from a generic base ontology plus the locked system shape. Workspace-specific requirements are data in the manifest, not application code.

## 9. Agent Integration Architecture

Agents are first-class bidirectional surfaces.

```text
System Book -> context packet -> agent prompt
agent session -> admission -> Episode -> ClaimProposal
```

### 9.1 Shared contract

Every platform plugin implements:

- installation and safe configuration merge;
- platform capability declaration;
- stable session-source reference;
- hook event normalization;
- transcript discovery/loading;
- transcript-to-canonical-event projection;
- context-delivery method;
- write-back scheduling;
- uninstall/diagnostic reporting.

The canonical agent event model contains only session metadata and human/agent text. Tool logs, hidden prompts, secrets, and command chatter are excluded before admission.

### 9.2 Per-agent plugins

OpenFlyWheel follows the useful shape in Greplica's platform installers rather than pretending every agent exposes the same lifecycle.

- **Claude Code:** install skills and merge lifecycle hooks into user settings without replacing existing handlers. Parse Claude JSONL into canonical session events.
- **Cursor:** install user skills, a generated always-applied project rule, and supported hooks. Cursor's pre-submit hook cannot inject arbitrary prompt context, so the rule instructs context retrieval while hooks record lifecycle events.
- **Codex:** install skills and merge its hook configuration. Parse Codex session metadata and message events through a Codex-specific adapter.
- **OpenClaw:** implement its native plugin/hook contract as a separate adapter after validating the installed runtime's current API.
- **MCP:** expose the same Book application services to Claude desktop/web and any platform without sufficient hooks.

Platform code owns configuration paths and transcript formats. It does not own Book logic.

### 9.3 Foreground and background behavior

Foreground hooks must be fast and best-effort:

- record session activity;
- return or point to a compact context packet;
- schedule any expensive extraction;
- never block the agent because background consolidation failed.

A leased background worker:

- loads the platform-specific transcript;
- projects it to canonical evidence;
- applies admission and redaction;
- creates an episode;
- generates claim proposals;
- never directly applies claims.

Hook recursion is disabled when OpenFlyWheel invokes an agent for background extraction.

### 9.4 Agent operations

- `book_context(query, workspace, boundary, pin?)`
- `book_get(claim_id, pin?)`
- `coverage_gaps(section?)`
- `episode_record(session_envelope)`
- `claim_propose(what, evidence)`
- `correction_record(claim_id, correction, evidence)`

CLI and MCP expose the same operations.

## 10. Retrieval

v0 retrieval is deterministic and does not require an LLM.

1. Resolve identity and workspace.
2. Apply ACL filtering before ranking.
3. Select claims active at the requested pin or time.
4. Retrieve candidates with SQLite FTS5.
5. Optionally add embedding candidates when configured.
6. Expand only direct evidence, conflict, and supersession edges.
7. Assemble a compact Markdown packet.

The packet contains:

- current verified claims;
- evidence anchors;
- relevant unresolved conflicts;
- known coverage gaps;
- pin/snapshot identity;
- no unsupported `Why` assertions.

## 11. Storage and Technology

- Python 3.11+
- Pydantic for contracts and validation
- SQLite with FTS5 for the local store
- Typer for the `ofw` CLI
- FastAPI for the local read API and later shared service
- MCP Python SDK for the agent server
- pytest, Ruff, and strict mypy

Embeddings are optional. All v0 acceptance tests must pass without credentials or network access.

SQLite tables cover workspaces, identities, boundaries, sources, episodes, evidence anchors, proposals, claims, edges, coverage requirements, connector checkpoints, agent sessions, and pins.

## 12. Repository Shape

```text
OpenFlyWheel/
  src/openflywheel/
    contracts/       # I/O-free typed records
    onboarding/      # workspace -> connect -> locate -> lock -> bootstrap
    connectors/
      github/
      agents/
        base.py
        claude.py
        cursor.py
        codex.py
        openclaw.py
    ingest/          # admission, episodes, SaO, grounded extraction
    book/            # proposals, verification, reconciliation, pins
    retrieval/       # ACL, FTS, graph expansion, packet rendering
    store/           # SQLite repositories and migrations
    cli/             # ofw
    mcp/             # same application services as CLI
    dashboard/       # read-only API/UI
  tests/
  fixtures/
  docs/specs/
  docs/plans/
```

## 13. Safety and Error Handling

- Fail closed when ACL or identity metadata is missing.
- Never persist connector credentials in episodes, logs, or proposals.
- Permission scope cannot widen during extraction, reconciliation, pinning, or retrieval.
- Prompt injection in source material is treated as evidence text.
- Machine extraction cannot bypass the proposal state.
- Connector retries are idempotent.
- Connector failure leaves its checkpoint unchanged.
- Background extraction failure does not block the foreground agent.
- Unsupported evidence becomes an explicit coverage gap.
- Historical pins remain immutable.
- Generated platform configuration is marked and updated idempotently.
- Installers merge with user configuration and never overwrite unrelated hooks, rules, or skills.

## 14. v0 Acceptance Criteria

1. Core code and generic tests contain no Arceus-specific names or topology.
2. `ofw onboard` discovers one GitHub organization and proposes system boundaries.
3. A human can lock a boundary, shape, owners, KPI, source authorities, and exclusions.
4. GitHub historical bootstrap and incremental ingest are idempotent.
5. Claude Code and Cursor plugins install without overwriting existing configuration.
6. Both agents can receive relevant context and write admitted session episodes back.
7. Every active claim resolves to accessible evidence.
8. Contradictory claims remain inspectable and history is never deleted.
9. Two identities can receive different packets for the same query.
10. A historical pin is unchanged after later ingestion.
11. Coverage is reported per U1-U7 and as a macro-average.
12. The Arceus dogfood workspace produces an auditable initial Book with missing U5-U7 requirements explicitly marked.

## 15. First Dogfood Workspace

The Arceus-Inc workspace connects:

- the GitHub organization and selected repositories;
- Claude Code;
- Cursor;
- optional expert-note files.

Onboarding, not product code, discovers and records the stack boundary. The workspace's first KPI is U1-U7 verified coverage. Dogfood succeeds when an agent can answer cross-repository architecture and operations questions from an evidence-backed context packet and when uncovered experiential truths are visible as targeted expert gaps.

## 16. Later Surfaces

After Wave 0:

1. Notion and Linear
2. Slack only after same-query/two-identity permission isolation passes
3. Drive, Gmail, Calendar, and Granola
4. LinkedIn only if people/outbound knowledge becomes part of the selected system

Each new source is a connector and ontology extension, not a new Book implementation.

