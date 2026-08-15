"""Safe JSON object file reads for installer surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import TypeAdapter, ValidationError

from openflywheel.contracts.operation_result import OperationResult

T = TypeVar("T")


def read_json_object_file(
    path: Path,
    *,
    malformed_code: str,
    not_object_code: str,
    malformed_message: str,
    not_object_message: str,
    malformed_hint: str,
    not_object_hint: str,
    malformed_stop: str,
    not_object_stop: str,
) -> OperationResult[dict[str, object]]:
    if not path.exists():
        return OperationResult.success(summary="JSON object absent", data={})
    raw_bytes = path.read_bytes()
    try:
        parsed: object = json.loads(raw_bytes)
    except json.JSONDecodeError:
        return OperationResult.failure(
            code=malformed_code,
            message=malformed_message,
            root_cause_hint=malformed_hint,
            safe_retry=False,
            stop_condition=malformed_stop,
            artifacts=(str(path),),
        )
    if not isinstance(parsed, dict):
        return OperationResult.failure(
            code=not_object_code,
            message=not_object_message,
            root_cause_hint=not_object_hint,
            safe_retry=False,
            stop_condition=not_object_stop,
            artifacts=(str(path),),
        )
    return OperationResult.success(summary="JSON object loaded", data=parsed)


def read_validated_json_object_file(
    path: Path,
    adapter: TypeAdapter[T],
    *,
    malformed_code: str,
    not_object_code: str,
    schema_invalid_code: str,
    malformed_message: str,
    not_object_message: str,
    schema_invalid_message: str,
    malformed_hint: str,
    not_object_hint: str,
    schema_invalid_hint: str,
    malformed_stop: str,
    not_object_stop: str,
    schema_invalid_stop: str,
) -> OperationResult[dict[str, object]]:
    loaded = read_json_object_file(
        path,
        malformed_code=malformed_code,
        not_object_code=not_object_code,
        malformed_message=malformed_message,
        not_object_message=not_object_message,
        malformed_hint=malformed_hint,
        not_object_hint=not_object_hint,
        malformed_stop=malformed_stop,
        not_object_stop=not_object_stop,
    )
    if loaded.error is not None:
        return loaded
    assert loaded.data is not None
    try:
        adapter.validate_python(loaded.data)
    except ValidationError:
        return OperationResult.failure(
            code=schema_invalid_code,
            message=schema_invalid_message,
            root_cause_hint=schema_invalid_hint,
            safe_retry=False,
            stop_condition=schema_invalid_stop,
            artifacts=(str(path),) if path.exists() else (),
        )
    return OperationResult.success(summary="JSON object schema validated", data=loaded.data)
