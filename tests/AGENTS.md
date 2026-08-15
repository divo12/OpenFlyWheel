# tests/

Offline-first pytest suite. Default run excludes `e2e_real` and `live_github`.

```text
unit/              contracts, CLI parse, db validation, live-github stub
integration/       SQLite repos, migrations, UoW, onboarding, ingest scope
e2e/               CLI smoke tests with temp workspace homes
e2e_real/          opt-in real Cursor/Claude/Arceus read-only harness (not default CI)
helpers.py         shared onboarding helper (conftest imports fixtures)
conftest.py        workspace_home + fixture_root fixtures
```

## Installer path semantics

- **`target_home`** — agent user config root (e.g. temp `~/.cursor` or `~/.claude` copy). Claude Code merges hooks into `target_home/.claude/settings.json`. Cursor does **not** read global `~/.cursor/hooks.json` for install; real `hooks.json` is hashed read-only in `e2e_real` as a side-effect guard only.
- **`project_root`** — repository root receiving `.cursor/rules/openflywheel.mdc`, `.cursor/skills/openflywheel/SKILL.md`, and `.cursor/openflywheel-hooks.json`. CursorInstaller install/diagnostics/uninstall operate on this path. Real E2E seeds schema-valid foreign hooks there (from a compatible real project copy when present, otherwise from `fixtures/agent-transcripts/cursor-openflywheel-hooks-foreign.json`).

Use `FixtureCo` and `fixtures/tiny-system`. No forbidden product names in generic tests.

See `SAFETY.md` for live test operator requirements and `e2e_real/README.md` for real-environment gates.
