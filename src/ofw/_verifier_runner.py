"""Timed child-process entrypoint for a file-backed Python verifier."""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable
from typing import cast

from pydantic import TypeAdapter, ValidationError

from ofw.runtime import RunResult, VerifierResult

_RUN_ADAPTER: TypeAdapter[RunResult] = TypeAdapter(RunResult)
_VERIFIER_ADAPTER: TypeAdapter[VerifierResult] = TypeAdapter(VerifierResult)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    payload: str = sys.stdin.read()
    try:
        result = _RUN_ADAPTER.validate_json(payload)
    except ValidationError:
        return 2
    module = importlib.import_module(sys.argv[1])
    functions = tuple(
        function
        for name, function in inspect.getmembers(module, inspect.isfunction)
        if name == sys.argv[2]
    )
    if len(functions) != 1:
        return 2
    function: Callable[[RunResult], VerifierResult] = cast(
        Callable[[RunResult], VerifierResult],
        functions[0],
    )
    verified: VerifierResult = function(result)
    sys.stdout.write(_VERIFIER_ADAPTER.dump_json(verified).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
