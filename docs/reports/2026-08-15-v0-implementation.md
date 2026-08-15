# OpenFlyWheel v0 Implementation Report

**Date:** 2026-08-16 · **Version:** 0.1.0 · **Full report:** [2026-08-15-v0-implementation.html](2026-08-15-v0-implementation.html)

## Outcome

v0 implements Waves A–H: typed contracts, SQLite SoR (WAL+FK), staged onboarding, GitHub fixture ingest, SaO U3/U4 proposals, human verification, ACL-filtered retrieval, 8 frozen MCP verbs, Claude/Cursor installers, loopback dashboard, and opt-in `e2e_real` harness.

**Combo:** Pavo truth (U1–U7, What+How gold) + Hyper write path (episodes → proposals → verify) + GBrain habits (Workspace ⊥ Source ⊥ Boundary, gaps-on-read, frozen verbs).

## Quality gates (verified 2026-08-16)

| Gate | Result |
|------|--------|
| ruff format/check | pass |
| mypy src | 137 files, no issues |
| pytest (default) | **165 passed, 4 deselected** |
| pytest `-m e2e_real` | **4 passed, 165 deselected** |

## Real E2E highlights

- **Arceus** (`OFW_ARCEUS_ROOT=/Users/divyansh/Arceus`): 50-file copy, 500-file hash inventory unchanged → 12 episodes, 6 proposals (U3=4, U4=2), MCP parity
- **Cursor transcript:** episode +1, proposals +2 (worker), claims +0; source hash unchanged
- **`~/.cursor/hooks.json`:** hash-guarded only; incompatible schema not consumed

## Status

**Not committed · Not pushed.** Implementation on disk only; last commit `08e987c` (design doc).

## Links

- [Design spec](../specs/2026-08-15-system-book-design.md)
- [Implementation plan](../plans/2026-08-15-org-system-book.md)
- [README](../../README.md)
- [AGENTS.md](../../AGENTS.md)
- [e2e_real README](../../tests/e2e_real/README.md)
