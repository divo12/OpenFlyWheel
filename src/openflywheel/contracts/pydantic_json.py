"""Typed helpers for pydantic JSON serialization without Any leakage."""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from pydantic import JsonValue, TypeAdapter

_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_DICT_ADAPTER: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])


@runtime_checkable
class PydanticJsonModel(Protocol):
    def model_dump_json(self, *, exclude_none: bool = ...) -> str: ...


def render_pydantic_json(value: PydanticJsonModel) -> JsonValue:
    raw: object = json.loads(value.model_dump_json())
    return _JSON_ADAPTER.validate_python(raw)


def model_dump_object_dict(model: PydanticJsonModel) -> dict[str, object]:
    raw: object = json.loads(model.model_dump_json(exclude_none=True))
    return _DICT_ADAPTER.validate_python(raw)
