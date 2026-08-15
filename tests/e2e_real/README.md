# Opt-in real E2E harness for OpenFlyWheel agent surfaces

These tests are **excluded from default CI** via `@pytest.mark.e2e_real`.

## Safety gates

- Requires `OFW_RUN_E2E_REAL=1` — otherwise every test skips
- Uses pytest `tmp_path` and `monkeypatch` for all writes
- **Install cycle** — hashes real `~/.cursor/hooks.json` read-only (side-effect guard; incompatible schema is **not** parsed by CursorInstaller). Seeds `project_root/.cursor/openflywheel-hooks.json` from a compatible real project copy when present, otherwise from typed fixture `fixtures/agent-transcripts/cursor-openflywheel-hooks-foreign.json`. Runs actual `install` → `diagnostics` → `uninstall` on temp `project_root` only; asserts foreign hooks preserved, generated skill/rule removed, diagnostics `installed` true then false.
- **Transcript + episode** — deterministically picks one real parent-session Cursor transcript (`*.jsonl`, preferring OpenFlyWheel/Arceus paths); copies under temp `.cursor/projects/…`; records before/after episode/proposal/claim deltas; asserts episode delta = 1 and claim delta = 0; worker proposal delta ≥ 0 (≥ 1 only when transcript contains deterministic proposal phrases); links worker proposals to the new episode via `transcript:{episode_id}:*` idempotency keys and anchors.
- **Arceus locate/ingest** — optional `OFW_ARCEUS_ROOT`: copies a bounded subset (**max 50 files**, two locateable Python repos when available) to temp; runs onboard/locate/lock/ingest/extract on the copy via offline **local fixture filesystem adapter** (`FixtureGitHubClient`); bounded SHA-256 inventory (**max 500 files**) on the **real** root before/after must be identical. No kernel-level write guard beyond hash inventory.

## Environment

| Variable | Required | Purpose |
|----------|----------|---------|
| `OFW_RUN_E2E_REAL` | Yes (`1`) | Master enable switch |
| `CURSOR_HOME` | No | Real Cursor config root (default `~/.cursor`) |
| `OFW_ARCEUS_ROOT` | No | Product checkout for read-only locate inventory |
| `OFW_HOME` | Set by tests | Temp workspace home under `tmp_path` |

## Path semantics (installers)

- **`target_home`** — agent user config directory. Cursor E2E does not mutate real `~/.cursor`; global `hooks.json` is guarded by hash only.
- **`project_root`** — git repo root receiving `.cursor/rules`, `.cursor/skills`, and `.cursor/openflywheel-hooks.json`. Always a temp directory in tests. Uninstall removes only generated entries.

## Limitations

- Arceus ingest uses offline local fixture adapter, not live GitHub.
- Bounded copy: **50 files** max into temp fixture; real-root inventory hashes **500 files** max.
- Transcript selection is deterministic (sorted paths, OpenFlyWheel/Arceus preference) but content varies by machine.
- No kernel or filesystem write guard beyond before/after SHA-256 inventory on configured real roots.

## Run manually

```bash
export OFW_RUN_E2E_REAL=1
export OFW_ARCEUS_ROOT=/path/to/checkout   # optional
pytest -m e2e_real
```

Do not run in CI without explicit operator approval.
