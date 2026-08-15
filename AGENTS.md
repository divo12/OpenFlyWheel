# OpenFlyWheel — Agent Guidelines

Python 3.11+ System Book foundation. Follow the specs in `docs/specs/` and `docs/plans/`.

## Architecture

```text
contracts/     I/O-free Pydantic models, enums, NewType IDs, OperationResult[T]
store/         SQLite Database, migrations, internal_records (row DTOs), repos, UnitOfWork
onboarding/    staged workspace → connect → locate → lock (`stage.py` monotonicity)
connectors/    envelope, GitHub fixture/live stubs, agent connect stubs, notes
ingest/        admission, episodes, SaO extraction (see ingest/AGENTS.md)
book/          verify, coverage, pins (see book/AGENTS.md)
retrieval/     ACL, FTS5, packet (see retrieval/AGENTS.md)
application/   orchestration; BookApplication + workspace (see application/AGENTS.md)
cli/           Typer entry point `ofw`
```

## Hard rules

1. **No `typing.Any` or untyped `object`** in domain contracts, service APIs, or repository APIs. Use Pydantic models, dataclasses, enums, Protocols, tuples, NewType IDs, explicit DTOs.
2. **`dict` is quarantined** to raw JSON/library boundaries; convert immediately to typed models.
3. **Database:** parameterized SQL only; WAL + FK + busy timeout; migrations in `store/migrations/`; checkpoint advances only inside successful episode transaction.
4. **No Arceus/dream/chorus/horizon/lattice** strings in `src/` or generic tests. Use FixtureCo and `fixtures/tiny-system`.
5. **Operation outputs:** `OperationResult[T]` with status, summary, next_actions, artifacts; errors include code, message, root_cause_hint, safe_retry, stop_condition.
6. **UUID generation** in service layer, not SQL triggers.
7. **UTC datetimes** everywhere.
8. Modules under 500 lines.

## Quality gate

```bash
ruff format src tests && ruff check src tests && mypy src && pytest
```

## Test markers

- `e2e_real` — excluded by default
- `live_github` — excluded by default; requires `OFW_GITHUB_TOKEN`

## Current waves

A–F implemented: workspace init, onboarding+lock, GitHub ingest+episodes, SaO proposals, verify/claims/coverage/pins, deterministic retrieval packet.

G–H implemented: Claude Code + Cursor installers (install/diagnostics/uninstall), transcript write-back with admission gates, background worker with lease/retry/max-attempts, MCP stdio `--surface verbs` (8 frozen verbs), read-only loopback dashboard, expert notes with authorization gates, opt-in `e2e_real` harness.

## MCP

```bash
ofw serve --surface verbs --home <workspace-home>
```

Frozen verbs: `book_context`, `book_get`, `coverage_gaps`, `episode_record`, `claim_propose`, `correction_record`, `book_verify`, `book_pin`.

## Installer paths

- **`target_home`** — agent user config root passed to `--target-home` (must exist; never implicit `$HOME` in tests)
- **`project_root`** — repo root passed to `--project-root` for rules/skills/hooks; required for install, uninstall, and diagnostics
- Uninstall strips only generated markers; foreign hooks/skills/rules are preserved
- Platform CLI accepts `claude-code` (user-facing) and `claude_code` (internal enum)
