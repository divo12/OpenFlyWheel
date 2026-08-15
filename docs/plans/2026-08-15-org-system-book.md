# OpenFlyWheel v0 plan — org System Book

Status: Ready to implement  
Date: 2026-08-15  
Parent spec: [2026-08-15-system-book-design.md](../specs/2026-08-15-system-book-design.md)  
Store: SQLite + FTS5 (system of record)  
First dogfood: Arceus-Inc (whole org, names only in the workspace manifest)

This plan is the combo: **Pavo** truth, **Hyper** write path, **GBrain** org topology and packet habits. It does not fork GBrain. It does not start the harness tournament.

## 1. What we are building

One local Python app (`ofw`) that onboards a **company workspace**, locks **many production systems** inside it, turns GitHub + agent sessions into an auditable System Book, and hands agents a permission-filtered packet.

The product is org-wide system knowledge, not a personal CRM and not a wiki.

```text
company workspace          ← GBrain "brain" (one SQLite file)
  ├── source: github/org
  ├── source: claude-code
  ├── source: cursor
  └── boundaries (Pavo lock, many)
        ├── system A   U1–U7 book
        ├── system B   U1–U7 book
        └── system C   U1–U7 book
```

Org coverage = macro-average of per-boundary U1–U7 coverage. A fat architecture section on one repo cannot hide an empty operations section on another.

## 2. Combo decisions (locked)

| Decision | From | Choice |
|----------|------|--------|
| Unit of gold | Pavo | Verified `What` + `How` under U1–U7. `Why` is a probe. |
| Write path | Hyper | source → admit → Episode → extract → Proposal → verify → Claim |
| Who writes proposals | OFW, not Greplica/GBrain | SaO, then OFW grounded-LLM job, then expert. Coding agent is a source. |
| Who activates claims | Pavo / Hyper | Human or designated owner. Never the session agent. Never a dream cycle. |
| Org axes | GBrain, adapted | **Workspace ⊥ Source ⊥ Boundary** (three, not two) |
| System of record | OFW (not GBrain) | **SQLite**. Markdown packets and dashboard are views. |
| Compiled truth + timeline | GBrain habit | Packet = compiled claims. Timeline = episodes. Split is a view, not a git wiki. |
| Gaps on every read | GBrain `think.gaps` | Every `book_context` lists unknown required slots. |
| Read path | Pavo + approved spec | Deterministic FTS + edges. **No LLM on v0 read.** |
| Agent doors | Hyper + Greplica + GBrain | CLI canonical. Hooks where they work. MCP verbs everywhere else. |
| Zero-LLM first | Pavo SaO + GBrain link regexes | SaO before any model. Abstain if thin. |
| Fail-closed remote | GBrain + Hyper | Missing ACL/identity → refuse. Scope cannot widen. |
| Frozen verbs | GBrain MEMORY_VERBS, adapted | Book ops below. Additive-forever. |
| Store | Approved spec | SQLite/FTS5. Embeddings optional. No pgvector, no Graphiti. |
| Wave 0 connectors | Approved spec | GitHub + Claude Code + Cursor. Expert notes as episode type, not headline. |
| Not in v0 | All three, minus GBrain firehose | No Slack/Gmail/Notion. No dream auto-apply. No people/deal ontology. No tournament. |

## 3. Three axes

```text
--workspace   which company SQLite file
--source      which connector/repo inside it
--boundary    which locked production system
```

- **Workspace** = tenant, policy, admins, one DB file (`~/.openflywheel/<id>/book.sqlite`).
- **Source** = GitHub org/repo, agent plugin, or expert-note folder. Slugs and checkpoints are per source.
- **Boundary** = one locked system. Claims, coverage, and pins are per boundary. Cross-boundary query is explicit (`--boundary all` only after ACL filter).

Rule of thumb (from GBrain, tightened): if the *company* changes, new workspace. If the *evidence repo* changes, new source. If the *running system* changes, new boundary.

Arceus-Inc is one workspace. Onboarding may lock `arceus`, `dream`, `chorus`, `horizon`, `lattice` as boundaries in the **manifest only**. Those strings never appear in `src/openflywheel/` or generic tests.

## 4. Data model (SQLite)

One file per workspace. WAL mode. Foreign keys on. Migrations in `src/openflywheel/store/migrations/`.

| Table | Role |
|-------|------|
| `workspaces` | id, name, policy, created_at |
| `identities` | id, workspace, kind (human/agent), acl |
| `boundaries` | locked system, shape, owners, kpi, exclusions, manifest_version |
| `sources` | connector kind, capability report, permission metadata |
| `episodes` | immutable admitted item: text, acl, event_time, ingest_time, checksum, source_ref |
| `evidence_anchors` | episode + locator (sha, file:line, issue, transcript span) |
| `proposals` | untrusted what/how, section, proposer, anchors |
| `claims` | verified what+how, U1–U7, state, authority, valid_from/to, acl |
| `edges` | `derived_from` / `in_tension_with` / `supersedes` |
| `why_probes` | never gold |
| `coverage_requirements` | shape + boundary slots |
| `checkpoints` | per-source cursor; advance only after episode txn |
| `agent_sessions` | platform, session_ref, transcript pointer |
| `pins` | immutable as-of snapshot of claim ids + manifest version |
| `audit_rejects` | admission refusals without content |

FTS5 virtual table over active claim what/how + episode text (ACL column stored alongside for prefilter).

**SoR rule (anti-GBrain):** do not treat a git markdown vault as canonical. Export (`ofw book export`) may write compiled markdown for humans. Re-import is not the recovery path. Recovery is the SQLite file + pins. Tests must not require a brain repo.

## 5. Frozen book verbs

Same ops on CLI and MCP. Field names frozen; additions only.

| Verb | Analog | Does |
|------|--------|------|
| `book_context` | GBrain `recall` + `context_pack` | ACL → FTS → edges → Markdown packet + gaps |
| `book_get` | `entity` | One claim + anchors + edges + history |
| `coverage_gaps` | GBrain `gaps[]` | Required slots still empty, per boundary or org |
| `episode_record` | Hyper write-back | Admit a session/note envelope |
| `claim_propose` | GBrain `remember`, gated | Hint or extractor output. Never activates. |
| `book_verify` | human | Promote / reject / leave in tension |
| `correction_record` | Hyper correction | New episode + proposal against a claim |
| `book_pin` | Pavo pin | Immutable snapshot |

No `synthesize` verb in v0. That is GBrain’s LLM-on-read. Our packet *is* the synthesis: verified claims plus explicit gaps.

## 6. Write and read (one picture)

```text
WRITE (Hyper inside Pavo)
  GitHub item / agent transcript / expert note
    → connector envelope
    → fail-closed admission
    → Episode (immutable)
    → SaO and/or grounded LLM job   ← OFW writes the proposal
    → ClaimProposal + EvidenceAnchor
    → neighborhood edges
    → you verify
    → Claim

READ (deterministic)
  query + identity + workspace + boundary? + pin?
    → ACL first
    → active claims at pin
    → FTS5 (+ optional embeddings)
    → evidence / tension / supersession
    → packet: compiled claims | episode timeline | gaps
```

## 7. Repository to create

```text
OpenFlyWheel/
  pyproject.toml
  src/openflywheel/
    contracts/          # pydantic, I/O-free
    onboarding/         # workspace → connect → locate → lock → bootstrap
    connectors/github/
    connectors/agents/{base,claude,cursor,codex,openclaw}.py
    connectors/notes/   # expert markdown drop
    ingest/{admit,episode,sao,grounded,worker}.py
    book/{propose,verify,reconcile,pin,coverage}.py
    retrieval/{acl,fts,expand,packet}.py
    store/{db,migrate,repos}.py
    cli/                # typer: ofw
    mcp/                # same services
    dashboard/          # fastapi + static read-only
  tests/
  fixtures/tiny-system/ # generic fake company, no Arceus names
  docs/specs/
  docs/plans/
```

Python 3.11+, Pydantic v2, Typer, FastAPI, MCP SDK, pytest, Ruff, strict mypy. All v0 tests pass offline.

## 8. Implementation waves

Each wave ends with a gate. Do not start the next wave until the gate is green.

### Wave A — skeleton (1–2 days)

- `pyproject.toml`, package, `ofw --help`
- contracts: Workspace, Identity, Boundary, Source, Episode, Anchor, Proposal, Claim, Edge, Pin, CoverageRequirement
- SQLite migrate + empty workspace create
- `ofw workspace init --name fixture-co`

**Gate:** `pytest` creates a temp DB, inserts a workspace, reads it back. No network.

### Wave B — onboard and lock (2–3 days)

- Staged `ofw onboard` (resumable state in DB)
- Connect stubs report capability (github, claude, cursor, notes)
- Locate: deterministic scan of a **local fixture repo** proposes boundaries (directory/package/readme heuristics). LLM rank optional, cannot lock.
- Lock: human confirms boundary, shape, owners, one KPI, authorities, exclusions → versioned manifest row
- Multiple boundaries per workspace

**Gate:** fixture company with two locked boundaries. Unlock-less extract is refused.

### Wave C — GitHub + admission + episodes (3–4 days)

- GitHub connector: org/repos inventory, files, issues, PRs, commits (local fixture first; live API behind env)
- Admission: secrets, excluded paths, junk, idempotent `(source_id, content_hash)`
- Episode write in one transaction; checkpoint advances only after commit
- Rejects go to `audit_rejects` without body

**Gate:** ingest fixture twice → same episode ids. Excluded path never stored. Checkpoint unchanged on injected failure.

### Wave D — SaO proposals (2–3 days)

- SaO over admitted code/config: constants, deps, schemas, guards, tests, metric names
- Emit proposals under U3/U4 only, each with file:line anchors
- No claim rows yet

**Gate:** fixture yields ≥ N U3/U4 proposals, all anchored, zero U5–U7 fabrications.

### Wave E — verify, claims, edges, coverage (3 days)

- `ofw book verify` / reject / tension
- Claims + `derived_from` / `in_tension_with` / `supersedes`
- Coverage requirements from generic shape ontology + locked shape
- Per-boundary and org macro-average
- Pins immutable

**Gate:** verify two proposals; pin; ingest more; pin unchanged. Coverage ignores raw proposal count. U5–U7 report as gaps.

### Wave F — retrieval packet (2 days)

- `ofw book context "…"`
- ACL before FTS
- Packet sections: claims, anchors, tensions, gaps, pin id
- Two identities, same query, different packets (fixture ACL)

**Gate:** packet has no unverified Why. Gap list matches coverage. Second identity omits private claim.

### Wave G — agents (3–4 days)

- Shared `PlatformInstaller` (Greplica shape): merge-safe skills/hooks/rules
- Claude Code + Cursor only in this wave
- Foreground: record session, return/point at packet, schedule extract
- Background worker: transcript → episode → proposals only. Hook recursion off.
- MCP `--surface verbs` exposing the frozen book ops

**Gate:** install does not overwrite user hooks. Session write-back creates an episode, not a claim. MCP `book_context` equals CLI output for the same identity.

### Wave H — dashboard + dogfood (2–3 days)

- `ofw book view` on 127.0.0.1, read-only, U1–U7 per boundary + org meter
- Expert-note drop → high-authority episode → proposal
- Arceus-Inc **manifest** (outside `src/`): connect real GitHub org, lock discovered systems, run bootstrap
- Targeted questions for empty U2/U5/U6/U7

**Gate (product):** an agent answers a cross-repo question from a packet; experiential holes are questions, not fake completeness. Core tests still have zero Arceus names.

## 9. What we steal vs leave

**Steal**

- GBrain: workspace/source split, gap-on-every-packet, fail-closed remote, compiled+timeline *view*, frozen verb protocol, capability-reporting connectors
- Hyper: admit-don’t-mirror, immutable episodes, typed neighborhood edges, bidirectional agents, hooks + MCP
- Pavo: locate/lock, U1–U7, What+How gold, SaO, machines on U3/U4, experts on U5–U7, artifact-correct ≠ system-true

**Leave**

- GBrain markdown SoR, dream auto-apply, LLM `think` on read, person/company/deal types, pgvector, OpenClaw-first
- Greplica agent-written `working-memory.proposal.json`
- Hyper Slack/Gmail firehose and outbound automations
- Pavo / HDP tournament

## 10. CLI sketch (wave targets)

```text
ofw workspace init
ofw onboard                          # staged
ofw connect github|claude|cursor|notes
ofw locate
ofw lock --boundary <id>
ofw ingest run [--source …]
ofw book context "how is memory gated?"
ofw book get <claim_id>
ofw book propose --what … --anchor …
ofw book verify <proposal_id>
ofw coverage [--boundary …]
ofw book pin
ofw install --platform cursor|claude
ofw serve --surface verbs
ofw book view
```

## 11. Test policy

- Offline default. Live GitHub tests marked and skipped without `OFW_GITHUB_TOKEN`.
- Fixture company: `fixtures/tiny-system/` with two tiny repos and two identities.
- Engine-free contracts tested without SQLite where possible.
- ACL tests are mandatory, not optional.
- No Arceus / dream / chorus / horizon / lattice strings under `src/` or `tests/` except an optional `tests/dogfood/` that is not in default CI.

## 12. Wave A first commit (when you say build)

1. `pyproject.toml` + `src/openflywheel/__init__.py`
2. Contracts module
3. SQLite migrations for workspaces/identities
4. `ofw workspace init`
5. One pytest

Stop there. Do not invent connectors in the same commit.
