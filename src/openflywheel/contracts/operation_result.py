"""Typed operation results for harness-compatible outputs."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from openflywheel.contracts.enums import OperationStatus
from openflywheel.contracts.pydantic_json import PydanticJsonModel, render_pydantic_json

T = TypeVar("T")


class OperationError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    root_cause_hint: str
    safe_retry: bool
    stop_condition: str


class OperationResult(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True)

    status: OperationStatus
    summary: str
    next_actions: tuple[str, ...] = Field(default_factory=tuple)
    artifacts: tuple[str, ...] = Field(default_factory=tuple)
    data: T | None = None
    error: OperationError | None = None

    @classmethod
    def success(
        cls,
        *,
        summary: str,
        data: T | None = None,
        next_actions: tuple[str, ...] = (),
        artifacts: tuple[str, ...] = (),
    ) -> OperationResult[T]:
        return cls(
            status=OperationStatus.SUCCESS,
            summary=summary,
            data=data,
            next_actions=next_actions,
            artifacts=artifacts,
        )

    @classmethod
    def warning(
        cls,
        *,
        summary: str,
        data: T | None = None,
        next_actions: tuple[str, ...] = (),
        artifacts: tuple[str, ...] = (),
    ) -> OperationResult[T]:
        return cls(
            status=OperationStatus.WARNING,
            summary=summary,
            data=data,
            next_actions=next_actions,
            artifacts=artifacts,
        )

    @classmethod
    def failure(
        cls,
        *,
        code: str,
        message: str,
        root_cause_hint: str,
        safe_retry: bool,
        stop_condition: str,
        next_actions: tuple[str, ...] = (),
        artifacts: tuple[str, ...] = (),
    ) -> OperationResult[T]:
        return cls(
            status=OperationStatus.ERROR,
            summary=message,
            next_actions=next_actions,
            artifacts=artifacts,
            error=OperationError(
                code=code,
                message=message,
                root_cause_hint=root_cause_hint,
                safe_retry=safe_retry,
                stop_condition=stop_condition,
            ),
        )

    def render_json(self) -> str:
        payload = _OperationRenderPayload(
            status=self.status.value,
            summary=self.summary,
            next_actions=self.next_actions,
            artifacts=self.artifacts,
            error=self.error,
            data=_render_data(self.data),
        )
        return payload.model_dump_json(indent=2, exclude_none=True)


class _OperationRenderPayload(BaseModel):
    status: str
    summary: str
    next_actions: tuple[str, ...] = Field(default_factory=tuple)
    artifacts: tuple[str, ...] = Field(default_factory=tuple)
    data: JsonValue | None = None
    error: OperationError | None = None


def _render_data(value: object) -> JsonValue | None:
    if value is None:
        return None
    if isinstance(value, PydanticJsonModel):
        return render_pydantic_json(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        rendered: list[JsonValue] = []
        for item in value:
            item_rendered = _render_data(item)
            if item_rendered is not None:
                rendered.append(item_rendered)
        return rendered
    msg = f"Unsupported operation result data type: {type(value).__name__}"
    raise TypeError(msg)
