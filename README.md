# OpenFlyWheel

Foundation for company workspace onboarding and System Book construction.

## Implemented scope (Waves A–H)

- Typed Pydantic v2 contracts (I/O-free)
- SQLite store with WAL, foreign keys, numbered migrations, repository + UnitOfWork patterns
- Staged onboarding, GitHub fixture ingest, SaO proposals, verify/claims/coverage/pins
- Deterministic retrieval packet with ACL
- **Wave G:** Claude Code + Cursor merge-safe installers, transcript projection, episode write-back, background worker with lease/retry, MCP stdio `--surface verbs` (8 frozen verbs)
- **Wave H:** read-only FastAPI dashboard (loopback bind only), expert notes connector with identity/source/boundary gates, opt-in `e2e_real` harness

## MCP server

Start the frozen verb surface over stdio:

```bash
ofw serve --surface verbs --home /path/to/workspace-home
```

**Frozen verbs (8):** `book_context`, `book_get`, `coverage_gaps`, `episode_record`, `claim_propose`, `correction_record`, `book_verify`, `book_pin`

Unknown tool names fail at the MCP protocol layer (`is_error=True`); they are not listed by `list_tools`.

## Agent install safety

`ofw install` **never** touches your real agent config unless you pass explicit paths:

```bash
ofw install --platform cursor --target-home /tmp/cursor-home --project-root /path/to/project
ofw install --platform claude-code --target-home /tmp/claude-home --project-root /path/to/project
ofw diagnostics --platform cursor --target-home /tmp/cursor-home
ofw uninstall --platform claude-code --target-home /tmp/claude-home --project-root /path/to/project
```

- **`target_home`** — agent user config root (`--target-home`). Claude merges hooks into `$target_home/.claude/settings.json`; Cursor uses it for diagnostics path validation. Never your live home in tests.
- **`project_root`** — repository root (`--project-root`) receiving `.cursor/rules`, `.claude/skills`, and hook JSON. Uninstall strips only generated markers; foreign hooks/skills/rules are preserved.

Platform CLI accepts `claude-code` (user-facing) and `claude_code` (internal enum value).

## Dashboard

```bash
ofw book view --home /path/to/workspace-home --host 127.0.0.1
```

Non-loopback hosts are rejected (`DASHBOARD_BIND_FORBIDDEN`).

## Quality commands

```bash
pip install -e ".[dev]"
ruff format src tests && ruff check src tests && mypy src && pytest
```

## Test markers

- `@pytest.mark.e2e_real` — real external system E2E (excluded by default; requires `OFW_RUN_E2E_REAL=1`)
- `@pytest.mark.live_github` — requires `OFW_GITHUB_TOKEN`

See `tests/e2e_real/README.md` and `tests/SAFETY.md`.

## Dogfood manifest

Example only (no secrets): `workspaces/arceus-inc/manifest.example.yaml`. Runtime DB/config paths are gitignored.
