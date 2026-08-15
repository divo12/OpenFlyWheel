"""Merge-safe Cursor skill, rule, and hook installer."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from openflywheel.connectors.agents.json_object import read_validated_json_object_file
from openflywheel.connectors.agents.path_guard import resolve_install_paths
from openflywheel.connectors.agents.platform import (
    PlatformInstaller,
    cursor_rule_content,
    generated_marker,
    skill_content,
)
from openflywheel.connectors.agents.settings_models import CursorHooksConfig
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.contracts.platform import (
    InstallArtifact,
    InstallDiagnostics,
    InstallSummary,
    PlatformCapability,
    UninstallSummary,
)

_HOOKS_ADAPTER: TypeAdapter[CursorHooksConfig] = TypeAdapter(CursorHooksConfig)
_HOOK_EVENTS: tuple[str, ...] = ("sessionStart", "sessionEnd")
_HOOKS_MALFORMED = {
    "malformed_code": "INSTALL_HOOKS_MALFORMED",
    "not_object_code": "INSTALL_HOOKS_NOT_OBJECT",
    "schema_invalid_code": "INSTALL_HOOKS_SCHEMA_INVALID",
    "malformed_message": "Cursor hooks JSON is not valid JSON",
    "not_object_message": "Cursor hooks JSON must be an object",
    "schema_invalid_message": "Cursor hooks JSON failed schema validation",
    "malformed_hint": "Repair hooks file manually before install",
    "not_object_hint": "Replace hooks root with an object",
    "schema_invalid_hint": "Fix hook entry types and structure",
    "malformed_stop": "Fix malformed .cursor/openflywheel-hooks.json",
    "not_object_stop": "Fix .cursor/openflywheel-hooks.json shape",
    "schema_invalid_stop": "Repair .cursor/openflywheel-hooks.json schema",
}


def _hook_command(event: str) -> str:
    return (
        "ofw agent hook --platform cursor --event "
        f'{event} --home "$OFW_HOME" --project-root "$OFW_PROJECT_ROOT"'
    )


def _read_hooks(path: Path) -> OperationResult[dict[str, object]]:
    return read_validated_json_object_file(path, _HOOKS_ADAPTER, **_HOOKS_MALFORMED)


def _failure_from_install(
    source: OperationResult[dict[str, object]],
) -> OperationResult[InstallSummary]:
    assert source.error is not None
    return OperationResult.failure(
        code=source.error.code,
        message=source.error.message,
        root_cause_hint=source.error.root_cause_hint,
        safe_retry=source.error.safe_retry,
        stop_condition=source.error.stop_condition,
        artifacts=source.artifacts,
    )


def _failure_from_uninstall(
    source: OperationResult[dict[str, object]],
) -> OperationResult[UninstallSummary]:
    assert source.error is not None
    return OperationResult.failure(
        code=source.error.code,
        message=source.error.message,
        root_cause_hint=source.error.root_cause_hint,
        safe_retry=source.error.safe_retry,
        stop_condition=source.error.stop_condition,
        artifacts=source.artifacts,
    )


def _failure_from_diagnostics(
    source: OperationResult[dict[str, object]],
) -> OperationResult[InstallDiagnostics]:
    assert source.error is not None
    return OperationResult.failure(
        code=source.error.code,
        message=source.error.message,
        root_cause_hint=source.error.root_cause_hint,
        safe_retry=source.error.safe_retry,
        stop_condition=source.error.stop_condition,
        artifacts=source.artifacts,
    )


class CursorInstaller:
    @property
    def capability(self) -> PlatformCapability:
        return PlatformCapability(
            platform=PlatformKind.CURSOR,
            supports_hooks=True,
            supports_skills=True,
            supports_rules=True,
            supports_mcp=True,
            transcript_format="jsonl",
            config_paths=(
                ".cursor/rules/openflywheel.mdc",
                ".cursor/skills/openflywheel/SKILL.md",
                ".cursor/openflywheel-hooks.json",
            ),
        )

    def install(
        self,
        *,
        target_home: str,
        project_root: str,
    ) -> OperationResult[InstallSummary]:
        paths = resolve_install_paths(target_home=target_home, project_root=project_root)
        if paths.error is not None:
            return OperationResult.failure(
                code=paths.error.code,
                message=paths.error.message,
                root_cause_hint=paths.error.root_cause_hint,
                safe_retry=paths.error.safe_retry,
                stop_condition=paths.error.stop_condition,
            )
        if paths.data is None:
            return OperationResult.failure(
                code="INSTALL_PATH_INTERNAL",
                message="Install path resolution failed",
                root_cause_hint="Report as internal error",
                safe_retry=False,
                stop_condition="Contact maintainers",
            )
        project = paths.data.project_root
        installed: list[str] = []
        merged: list[str] = []
        skipped: list[str] = []

        skill_path = project / ".cursor" / "skills" / "openflywheel" / "SKILL.md"
        if skill_path.exists():
            existing = skill_path.read_text(encoding="utf-8")
            if generated_marker(PlatformKind.CURSOR) not in existing:
                return OperationResult.failure(
                    code="INSTALL_COLLISION",
                    message="Refusing to overwrite foreign OpenFlyWheel skill file",
                    root_cause_hint="Remove or rename existing skill file",
                    safe_retry=False,
                    stop_condition="Resolve .cursor/skills/openflywheel/SKILL.md collision",
                )

        rule_path = project / ".cursor" / "rules" / "openflywheel.mdc"
        if rule_path.exists():
            existing_rule = rule_path.read_text(encoding="utf-8")
            if generated_marker(PlatformKind.CURSOR) not in existing_rule:
                return OperationResult.failure(
                    code="INSTALL_COLLISION",
                    message="Refusing to overwrite foreign OpenFlyWheel rule file",
                    root_cause_hint="Remove or rename existing rule file",
                    safe_retry=False,
                    stop_condition="Resolve .cursor/rules/openflywheel.mdc collision",
                )

        hooks_path = project / ".cursor" / "openflywheel-hooks.json"
        hooks_raw = _read_hooks(hooks_path)
        if hooks_raw.error is not None:
            return _failure_from_install(hooks_raw)
        assert hooks_raw.data is not None

        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_text = skill_content(PlatformKind.CURSOR)
        if skill_path.exists() and skill_path.read_text(encoding="utf-8") == skill_text:
            skipped.append(str(skill_path))
        else:
            skill_path.write_text(skill_text, encoding="utf-8")
            installed.append(str(skill_path))

        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_text = cursor_rule_content()
        if rule_path.exists() and rule_path.read_text(encoding="utf-8") == rule_text:
            skipped.append(str(rule_path))
        else:
            rule_path.write_text(rule_text, encoding="utf-8")
            installed.append(str(rule_path))

        config, changed = _merge_hooks_dict(hooks_raw.data, hooks_path=hooks_path)
        if changed:
            hooks_path.parent.mkdir(parents=True, exist_ok=True)
            hooks_path.write_text(
                json.dumps(config, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            merged.append(str(hooks_path))
        elif hooks_path.exists():
            skipped.append(str(hooks_path))

        return OperationResult.success(
            summary=f"Installed Cursor OpenFlyWheel surfaces in {project_root}",
            data=InstallSummary(
                platform=PlatformKind.CURSOR,
                installed_paths=tuple(installed),
                merged_files=tuple(merged),
                skipped_existing=tuple(skipped),
            ),
            next_actions=("Set OFW_HOME and OFW_IDENTITY in shell profile",),
        )

    def uninstall(
        self,
        *,
        target_home: str,
        project_root: str,
    ) -> OperationResult[UninstallSummary]:
        paths = resolve_install_paths(target_home=target_home, project_root=project_root)
        if paths.error is not None:
            return OperationResult.failure(
                code=paths.error.code,
                message=paths.error.message,
                root_cause_hint=paths.error.root_cause_hint,
                safe_retry=paths.error.safe_retry,
                stop_condition=paths.error.stop_condition,
            )
        if paths.data is None:
            return OperationResult.failure(
                code="INSTALL_PATH_INTERNAL",
                message="Uninstall path resolution failed",
                root_cause_hint="Report as internal error",
                safe_retry=False,
                stop_condition="Contact maintainers",
            )
        project = paths.data.project_root
        removed: list[str] = []
        restored: list[str] = []

        hooks_path = project / ".cursor" / "openflywheel-hooks.json"
        hooks_raw: dict[str, object] | None = None
        if hooks_path.exists():
            raw_result = _read_hooks(hooks_path)
            if raw_result.error is not None:
                return _failure_from_uninstall(raw_result)
            hooks_raw = raw_result.data

        for rel in (
            ".cursor/skills/openflywheel/SKILL.md",
            ".cursor/rules/openflywheel.mdc",
        ):
            path = project / rel
            if path.exists():
                text = path.read_text(encoding="utf-8")
                if generated_marker(PlatformKind.CURSOR) in text:
                    path.unlink()
                    removed.append(str(path))
                    if path.parent.exists() and not any(path.parent.iterdir()):
                        path.parent.rmdir()

        if hooks_path.exists() and hooks_raw is not None:
            cleaned, changed = _strip_generated_hooks(hooks_raw)
            if changed:
                if cleaned:
                    hooks_path.write_text(
                        json.dumps(cleaned, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    restored.append(str(hooks_path))
                else:
                    hooks_path.unlink()
                    removed.append(str(hooks_path))

        return OperationResult.success(
            summary="Removed Cursor OpenFlyWheel generated artifacts",
            data=UninstallSummary(
                platform=PlatformKind.CURSOR,
                removed_paths=tuple(removed),
                restored_files=tuple(restored),
            ),
        )

    def diagnostics(
        self,
        *,
        target_home: str,
        project_root: str,
    ) -> OperationResult[InstallDiagnostics]:
        paths = resolve_install_paths(target_home=target_home, project_root=project_root)
        if paths.error is not None:
            return OperationResult.failure(
                code=paths.error.code,
                message=paths.error.message,
                root_cause_hint=paths.error.root_cause_hint,
                safe_retry=paths.error.safe_retry,
                stop_condition=paths.error.stop_condition,
            )
        if paths.data is None:
            return OperationResult.failure(
                code="INSTALL_PATH_INTERNAL",
                message="Diagnostics path resolution failed",
                root_cause_hint="Report as internal error",
                safe_retry=False,
                stop_condition="Contact maintainers",
            )
        project = paths.data.project_root
        artifacts: list[InstallArtifact] = []
        warnings: list[str] = []
        installed = False
        marker = generated_marker(PlatformKind.CURSOR)

        for rel, action in (
            (".cursor/skills/openflywheel/SKILL.md", "installed"),
            (".cursor/rules/openflywheel.mdc", "installed"),
        ):
            path = project / rel
            if path.exists() and marker in path.read_text(encoding="utf-8"):
                installed = True
                artifacts.append(InstallArtifact(path=str(path), action=action))

        hooks_path = project / ".cursor" / "openflywheel-hooks.json"
        if hooks_path.exists():
            raw_result = _read_hooks(hooks_path)
            if raw_result.error is not None:
                return _failure_from_diagnostics(raw_result)
            assert raw_result.data is not None
            hooks = raw_result.data.get("hooks")
            if isinstance(hooks, list) and _has_generated_command(hooks):
                installed = True
                artifacts.append(InstallArtifact(path=str(hooks_path), action="merged"))
        else:
            warnings.append("Cursor openflywheel-hooks.json not found")

        return OperationResult.success(
            summary="Cursor diagnostics complete",
            data=InstallDiagnostics(
                platform=PlatformKind.CURSOR,
                installed=installed,
                artifacts=tuple(artifacts),
                warnings=tuple(warnings),
            ),
        )


def _merge_hooks_dict(
    raw: dict[str, object],
    *,
    hooks_path: Path,
) -> tuple[dict[str, object], bool]:
    validated = _HOOKS_ADAPTER.validate_python(raw)
    hooks = [
        {"event": entry.event, "command": entry.command}
        for entry in validated.hooks
        if generated_marker(PlatformKind.CURSOR) not in entry.command
    ]
    changed = False
    for event in _HOOK_EVENTS:
        command = _hook_command(event)
        if any(entry["command"] == command for entry in hooks):
            continue
        hooks.append({"event": event, "command": command})
        changed = True
    payload = {"version": 1, "hooks": hooks, "_generated": generated_marker(PlatformKind.CURSOR)}
    if not changed and hooks_path.exists():
        return raw, False
    return payload, True


def _strip_generated_hooks(raw: dict[str, object]) -> tuple[dict[str, object], bool]:
    validated = _HOOKS_ADAPTER.validate_python(raw)
    marker = generated_marker(PlatformKind.CURSOR)
    kept: list[dict[str, object]] = []
    changed = False
    for entry in validated.hooks:
        payload: dict[str, object] = {"event": entry.event, "command": entry.command}
        if marker in entry.command or "ofw agent hook" in entry.command:
            changed = True
            continue
        kept.append(payload)
    if not changed:
        return raw, False
    if not kept:
        return {}, True
    merged = dict(raw)
    merged["hooks"] = kept
    merged.pop("_generated", None)
    return merged, True


def _has_generated_command(entries: list[object]) -> bool:
    marker = generated_marker(PlatformKind.CURSOR)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        if isinstance(command, str) and (marker in command or "ofw agent hook" in command):
            return True
    return False


def get_cursor_installer() -> PlatformInstaller:
    return CursorInstaller()
