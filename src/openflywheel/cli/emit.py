"""Emit OperationResult to stdout and set exit code."""

from __future__ import annotations

from typing import TypeVar

import typer

from openflywheel.cli.render import render_operation_result
from openflywheel.contracts.enums import OperationStatus
from openflywheel.contracts.operation_result import OperationResult

T = TypeVar("T")


def emit_result(result: OperationResult[T]) -> None:
    typer.echo(render_operation_result(result))
    if result.status == OperationStatus.ERROR:
        raise typer.Exit(code=1)
