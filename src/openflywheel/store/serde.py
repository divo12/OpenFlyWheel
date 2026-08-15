"""JSON serialization at storage boundaries."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, TypeAdapter

T = TypeVar("T", bound=BaseModel)


def model_to_json(model: BaseModel) -> str:
    return model.model_dump_json()


def model_from_json(model_type: type[T], payload: str) -> T:
    return model_type.model_validate_json(payload)


def tuple_to_json(items: tuple[str, ...]) -> str:
    adapter: TypeAdapter[list[str]] = TypeAdapter(list[str])
    return adapter.dump_json(list(items)).decode("utf-8")


def tuple_from_json(payload: str) -> tuple[str, ...]:
    adapter: TypeAdapter[list[str]] = TypeAdapter(list[str])
    parsed = adapter.validate_json(payload)
    return tuple(parsed)
