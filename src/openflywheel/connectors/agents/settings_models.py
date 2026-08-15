"""Typed platform settings models (raw JSON quarantined at adapter boundary)."""

from pydantic import BaseModel, ConfigDict, Field


class ClaudeHookCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    type: str = "command"
    command: str


class ClaudeHookEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    hooks: tuple[ClaudeHookCommand, ...] = Field(default_factory=tuple)


class ClaudeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    hooks: dict[str, tuple[ClaudeHookEntry, ...]] | None = None


class CursorHookCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    command: str


class CursorHookEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    event: str
    command: str


class CursorHooksConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    version: int = 1
    hooks: tuple[CursorHookEntry, ...] = Field(default_factory=tuple)


class CursorSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    hooks: CursorHooksConfig | None = None
