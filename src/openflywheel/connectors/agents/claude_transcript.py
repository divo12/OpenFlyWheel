"""Claude Code JSONL transcript projection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from openflywheel.contracts.agent_session import CanonicalAgentSession, CanonicalMessage
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import AgentSessionId

_RAW_LINE_ADAPTER: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])

_EXCLUDED_TYPES: frozenset[str] = frozenset(
    {
        "tool_use",
        "tool_result",
        "system",
        "progress",
        "command",
        "thinking",
        "tool",
    }
)


class ClaudeTranscriptLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    type: str | None = None
    timestamp: str | None = None
    message: dict[str, object] | None = None


def load_claude_transcript(
    path: Path,
    *,
    session_ref: str,
    session_id: AgentSessionId,
    project_root: str | None = None,
) -> CanonicalAgentSession:
    messages: list[CanonicalMessage] = []
    started_at: datetime | None = None
    index = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed: object = json.loads(stripped)
        except json.JSONDecodeError:
            raise
        raw = _RAW_LINE_ADAPTER.validate_python(parsed)
        line_type = raw.get("type")
        if not isinstance(line_type, str):
            continue
        if line_type in _EXCLUDED_TYPES:
            continue
        if line_type not in {"user", "assistant"}:
            continue
        message_obj = raw.get("message")
        if not isinstance(message_obj, dict):
            continue
        role = message_obj.get("role")
        text = _extract_text(message_obj.get("content"))
        if not isinstance(role, str) or not text.strip():
            continue
        event_time = _parse_timestamp(raw.get("timestamp"))
        if started_at is None and event_time is not None:
            started_at = event_time
        messages.append(CanonicalMessage(role=role, text=text.strip(), message_index=index))
        index += 1

    return CanonicalAgentSession(
        session_id=session_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref=session_ref,
        project_root=project_root,
        started_at=started_at,
        messages=tuple(messages),
    )


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
