"""Transcript discovery and platform-specific loading."""

from __future__ import annotations

import json
from pathlib import Path

from openflywheel.connectors.agents.claude_transcript import load_claude_transcript
from openflywheel.connectors.agents.cursor_transcript import load_cursor_transcript
from openflywheel.connectors.agents.path_guard import resolve_transcript_path
from openflywheel.contracts.agent_session import CanonicalAgentSession
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import AgentSessionId
from openflywheel.contracts.operation_result import OperationResult


def load_canonical_session(
    *,
    platform: PlatformKind,
    transcript_path: Path,
    session_ref: str,
    session_id: AgentSessionId,
    project_root: str | None = None,
    allowed_roots: tuple[Path, ...],
) -> OperationResult[CanonicalAgentSession]:
    validated = resolve_transcript_path(
        transcript_path=str(transcript_path),
        allowed_roots=allowed_roots,
    )
    if validated.error is not None:
        return OperationResult.failure(
            code=validated.error.code,
            message=validated.error.message,
            root_cause_hint=validated.error.root_cause_hint,
            safe_retry=validated.error.safe_retry,
            stop_condition=validated.error.stop_condition,
        )
    if validated.data is None:
        return OperationResult.failure(
            code="TRANSCRIPT_INTERNAL",
            message="Validated transcript path missing",
            root_cause_hint="Report as internal error",
            safe_retry=False,
            stop_condition="Contact maintainers",
        )
    safe_path = validated.data.path
    try:
        if platform == PlatformKind.CLAUDE_CODE:
            session = load_claude_transcript(
                safe_path,
                session_ref=session_ref,
                session_id=session_id,
                project_root=project_root,
            )
        else:
            session = load_cursor_transcript(
                safe_path,
                session_ref=session_ref,
                session_id=session_id,
                project_root=project_root,
            )
    except json.JSONDecodeError:
        return OperationResult.failure(
            code="TRANSCRIPT_MALFORMED",
            message="Transcript JSONL is malformed",
            root_cause_hint="Repair or replace transcript file",
            safe_retry=False,
            stop_condition="Provide valid JSONL transcript",
        )
    return OperationResult.success(summary="Transcript loaded", data=session)


def discover_transcript_path(
    *,
    platform: PlatformKind,
    target_home: Path,
    session_ref: str,
) -> Path | None:
    if platform == PlatformKind.CLAUDE_CODE:
        candidate = target_home / ".claude" / "projects" / session_ref / "transcript.jsonl"
        return candidate if candidate.exists() else None
    candidate = target_home / ".cursor" / "chats" / f"{session_ref}.jsonl"
    return candidate if candidate.exists() else None
