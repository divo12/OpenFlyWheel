"""Cursor JSONL transcript projection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from openflywheel.contracts.agent_session import CanonicalAgentSession, CanonicalMessage
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import AgentSessionId

_RAW_LINE_ADAPTER: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])

_EXCLUDED_TYPES: frozenset[str] = frozenset(
    {
        "tool_call",
        "tool_result",
        "system",
        "hidden",
        "command",
        "thinking",
    }
)


def load_cursor_transcript(
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
        parsed: object = json.loads(stripped)
        raw = _RAW_LINE_ADAPTER.validate_python(parsed)
        line_type = raw.get("type")
        if isinstance(line_type, str) and line_type in _EXCLUDED_TYPES:
            continue
        role = raw.get("role")
        if not isinstance(role, str):
            kind = raw.get("type")
            if kind == "user":
                role = "user"
            elif kind == "assistant":
                role = "assistant"
            else:
                continue
        text = _extract_text(raw)
        if not text.strip():
            continue
        event_time = _parse_timestamp(raw.get("timestamp"))
        if started_at is None and event_time is not None:
            started_at = event_time
        messages.append(CanonicalMessage(role=role, text=text.strip(), message_index=index))
        index += 1

    return CanonicalAgentSession(
        session_id=session_id,
        platform=PlatformKind.CURSOR,
        session_ref=session_ref,
        project_root=project_root,
        started_at=started_at,
        messages=tuple(messages),
    )


def _extract_text(raw: dict[str, object]) -> str:
    for key in ("text", "content", "message"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("content")
            if isinstance(nested, str):
                return nested
            if isinstance(nested, list):
                parts: list[str] = []
                for item in nested:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                return "\n".join(parts)
    return ""


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
