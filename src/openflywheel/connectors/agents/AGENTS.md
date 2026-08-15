# connectors/agents/

Merge-safe Claude Code and Cursor installers plus transcript projection.

## Installer path semantics

- **`target_home`** — agent user config root (`--target-home`). Claude merges hooks into `$target_home/.claude/settings.json`; Cursor validates it for diagnostics. Must be an existing directory.
- **`project_root`** — repository root (`--project-root`); generated skills/rules/hook JSON are written here. Required for install, uninstall, and diagnostics.

All three entry points call `resolve_install_paths` for path confinement. Foreign skill/rule collisions fail closed (`INSTALL_COLLISION`). Malformed or non-object hooks JSON fails without corrupting bytes (`INSTALL_HOOKS_MALFORMED`, `INSTALL_HOOKS_NOT_OBJECT`). Uninstall strips only generated markers; foreign hooks/skills/rules are preserved.

Diagnostics inspect **`project_root`** artifacts and report `installed=True` when generated artifacts are present.

Platform CLI accepts `claude-code` (user-facing alias) and `claude_code` (internal enum).

## Transcript loading

Episode services receive explicit **`agent_home`** and **`project_root`** (never infer roots from the transcript parent). Trusted roots allow platform config dirs (`.claude`/`.cursor`) while rejecting sensitive hidden paths (`.env`, `.ssh`, etc.), traversal, and path separators in `session_ref`. Real discovered transcript paths under trusted roots must load.

Platforms: Claude Code, Cursor.
