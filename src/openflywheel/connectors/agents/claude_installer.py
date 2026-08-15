"""Merge-safe Claude Code skill and hook installer."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from openflywheel.connectors.agents.json_object import read_validated_json_object_file
from openflywheel.connectors.agents.path_guard import resolve_install_paths
from openflywheel.connectors.agents.platform import (
    PlatformInstaller,
    generated_marker,
    skill_content,
)
from openflywheel.connectors.agents.settings_models import ClaudeSettings
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.contracts.platform import (
    InstallArtifact,
    InstallDiagnostics,
    InstallSummary,
    PlatformCapability,
    UninstallSummary,
)
from openflywheel.contracts.pydantic_json import model_dump_object_dict

_HOOK_EVENTS: tuple[str, ...] = ("SessionStart", "Stop")
_SETTINGS_ADAPTER: TypeAdapter[ClaudeSettings] = TypeAdapter(ClaudeSettings)
_SETTINGS_JSON = {
    "malformed_code": "INSTALL_SETTINGS_MALFORMED",
    "not_object_code": "INSTALL_SETTINGS_NOT_OBJECT",
    "schema_invalid_code": "INSTALL_SETTINGS_SCHEMA_INVALID",
    "malformed_message": "Claude settings.json is not valid JSON",
    "not_object_message": "Claude settings.json must be a JSON object",
    "schema_invalid_message": "Claude settings.json failed schema validation",
    "malformed_hint": "Repair settings.json manually before install",
    "not_object_hint": "Replace settings root with an object",
    "schema_invalid_hint": "Fix hook entry types and structure",
    "malformed_stop": "Fix malformed Claude settings.json",
    "not_object_stop": "Fix Claude settings.json shape",
    "schema_invalid_stop": "Repair Claude settings.json schema",
}


def _hook_command(event: str) -> str:
    return (
        "ofw agent hook --platform claude_code --event "
        f'{event} --home "$OFW_HOME" --project-root "$OFW_PROJECT_ROOT"'
    )


def _read_settings(path: Path) -> OperationResult[dict[str, object]]:
    return read_validated_json_object_file(path, _SETTINGS_ADAPTER, **_SETTINGS_JSON)


def _failure_from(source: OperationResult[dict[str, object]]) -> OperationResult[InstallSummary]:
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


class ClaudeCodeInstaller:
    @property
    def capability(self) -> PlatformCapability:
        return PlatformCapability(
            platform=PlatformKind.CLAUDE_CODE,
            supports_hooks=True,
            supports_skills=True,
            supports_rules=False,
            supports_mcp=True,
            transcript_format="jsonl",
            config_paths=(".claude/settings.json", ".claude/skills/openflywheel/SKILL.md"),
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
        home = paths.data.target_home
        project = paths.data.project_root
        installed: list[str] = []
        merged: list[str] = []
        skipped: list[str] = []

        skill_path = project / ".claude" / "skills" / "openflywheel" / "SKILL.md"
        if skill_path.exists():
            existing = skill_path.read_text(encoding="utf-8")
            if generated_marker(PlatformKind.CLAUDE_CODE) not in existing:
                return OperationResult.failure(
                    code="INSTALL_COLLISION",
                    message="Refusing to overwrite foreign OpenFlyWheel skill file",
                    root_cause_hint="Remove or rename existing skill file",
                    safe_retry=False,
                    stop_condition="Resolve .claude/skills/openflywheel/SKILL.md collision",
                )

        settings_path = home / ".claude" / "settings.json"
        settings_raw = _read_settings(settings_path)
        if settings_raw.error is not None:
            return _failure_from(settings_raw)
        assert settings_raw.data is not None

        skill_path.parent.mkdir(parents=True, exist_ok=True)
        content = skill_content(PlatformKind.CLAUDE_CODE)
        if skill_path.exists() and skill_path.read_text(encoding="utf-8") == content:
            skipped.append(str(skill_path))
        else:
            skill_path.write_text(content, encoding="utf-8")
            installed.append(str(skill_path))

        settings_path.parent.mkdir(parents=True, exist_ok=True)
        updated, changed = _merge_hooks(settings_raw.data, _project_root=project_root)
        if changed:
            settings_path.write_text(
                json.dumps(updated, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            merged.append(str(settings_path))
        elif settings_path.exists():
            skipped.append(str(settings_path))

        return OperationResult.success(
            summary=f"Installed Claude Code OpenFlyWheel surfaces in {project_root}",
            data=InstallSummary(
                platform=PlatformKind.CLAUDE_CODE,
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
        home = paths.data.target_home
        project = paths.data.project_root
        removed: list[str] = []
        restored: list[str] = []

        settings_path = home / ".claude" / "settings.json"
        settings_raw: dict[str, object] | None = None
        if settings_path.exists():
            raw_result = _read_settings(settings_path)
            if raw_result.error is not None:
                return _failure_from_uninstall(raw_result)
            settings_raw = raw_result.data

        skill_path = project / ".claude" / "skills" / "openflywheel" / "SKILL.md"
        if skill_path.exists():
            text = skill_path.read_text(encoding="utf-8")
            if generated_marker(PlatformKind.CLAUDE_CODE) in text:
                skill_path.unlink()
                removed.append(str(skill_path))
                if skill_path.parent.exists() and not any(skill_path.parent.iterdir()):
                    skill_path.parent.rmdir()

        if settings_path.exists() and settings_raw is not None:
            cleaned, changed = _strip_generated_hooks(settings_raw)
            if changed:
                settings_path.write_text(
                    json.dumps(cleaned, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                restored.append(str(settings_path))

        return OperationResult.success(
            summary="Removed Claude Code OpenFlyWheel generated artifacts",
            data=UninstallSummary(
                platform=PlatformKind.CLAUDE_CODE,
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
        home = paths.data.target_home
        project = paths.data.project_root
        artifacts: list[InstallArtifact] = []
        warnings: list[str] = []
        settings_path = home / ".claude" / "settings.json"
        installed = False
        if settings_path.exists():
            raw_result = _read_settings(settings_path)
            if raw_result.error is not None:
                return _failure_from_diagnostics(raw_result)
            assert raw_result.data is not None
            hooks = raw_result.data.get("hooks")
            if isinstance(hooks, dict):
                for event in _HOOK_EVENTS:
                    entries = hooks.get(event)
                    if isinstance(entries, list) and _has_generated_command(entries):
                        installed = True
                        artifacts.append(InstallArtifact(path=str(settings_path), action="merged"))
        else:
            warnings.append("Claude settings.json not found")

        skill_path = project / ".claude" / "skills" / "openflywheel" / "SKILL.md"
        if skill_path.exists() and generated_marker(
            PlatformKind.CLAUDE_CODE
        ) in skill_path.read_text(encoding="utf-8"):
            installed = True
            artifacts.append(InstallArtifact(path=str(skill_path), action="installed"))

        return OperationResult.success(
            summary="Claude Code diagnostics complete",
            data=InstallDiagnostics(
                platform=PlatformKind.CLAUDE_CODE,
                installed=installed,
                artifacts=tuple(artifacts),
                warnings=tuple(warnings),
            ),
        )


def _merge_hooks(
    raw: dict[str, object],
    *,
    _project_root: str,
) -> tuple[dict[str, object], bool]:
    validated = _SETTINGS_ADAPTER.validate_python(raw)
    hooks: dict[str, list[dict[str, object]]] = {}
    if validated.hooks is not None:
        for event, entries in validated.hooks.items():
            hooks[event] = [model_dump_object_dict(entry) for entry in entries]

    changed = False
    marker = generated_marker(PlatformKind.CLAUDE_CODE)
    for event in _HOOK_EVENTS:
        command = _hook_command(event)
        existing = hooks.get(event, [])
        if _has_command(existing, command):
            continue
        foreign = [entry for entry in existing if not _entry_has_marker(entry, marker)]
        generated: dict[str, object] = {
            "hooks": [{"type": "command", "command": command}],
            "_comment": marker,
        }
        hooks[event] = foreign + [generated]
        changed = True

    if not changed:
        return raw, False

    merged = dict(raw)
    merged["hooks"] = hooks
    return merged, True


def _strip_generated_hooks(raw: dict[str, object]) -> tuple[dict[str, object], bool]:
    validated = _SETTINGS_ADAPTER.validate_python(raw)
    if validated.hooks is None:
        return raw, False
    marker = generated_marker(PlatformKind.CLAUDE_CODE)
    cleaned_hooks: dict[str, list[dict[str, object]]] = {}
    changed = False
    for event, entries in validated.hooks.items():
        kept: list[dict[str, object]] = []
        for entry in entries:
            payload = model_dump_object_dict(entry)
            if _entry_has_marker(payload, marker):
                changed = True
                continue
            kept.append(payload)
        if kept:
            cleaned_hooks[event] = kept
    if not changed:
        return raw, False
    merged = dict(raw)
    if cleaned_hooks:
        merged["hooks"] = cleaned_hooks
    else:
        merged.pop("hooks", None)
    return merged, True


def _entry_has_marker(entry: dict[str, object], marker: str) -> bool:
    comment = entry.get("_comment")
    if isinstance(comment, str) and marker in comment:
        return True
    hooks = entry.get("hooks")
    if isinstance(hooks, list):
        for hook in hooks:
            if isinstance(hook, dict):
                command = hook.get("command")
                if isinstance(command, str) and "ofw agent hook" in command:
                    return True
    return False


def _has_generated_command(entries: list[object]) -> bool:
    marker = generated_marker(PlatformKind.CLAUDE_CODE)
    return any(isinstance(entry, dict) and _entry_has_marker(entry, marker) for entry in entries)


def _has_command(entries: list[dict[str, object]], command: str) -> bool:
    for entry in entries:
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            command_value = hook.get("command")
            if isinstance(command_value, str) and command_value == command:
                return True
    return False


def get_claude_installer() -> PlatformInstaller:
    return ClaudeCodeInstaller()
