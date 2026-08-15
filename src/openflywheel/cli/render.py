"""Render OperationResult for CLI output."""

from typing import TypeVar

from openflywheel.contracts.operation_result import OperationResult

T = TypeVar("T")


def render_operation_result(result: OperationResult[T]) -> str:
    return result.render_json()
