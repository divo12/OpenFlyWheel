"""Shared immutable OpenFlywheel value contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ComponentKind(StrEnum):
    PROMPT = "prompt"
    TOOL = "tool"
    SKILL = "skill"
    SUBAGENT = "subagent"
    MIDDLEWARE = "middleware"


@dataclass(frozen=True, slots=True)
class Sha256Digest:
    value: str

    def __str__(self) -> str:
        return self.value
