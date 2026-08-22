"""Timed child-process entrypoint for a file-backed trace diagnoser."""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable
from typing import cast

from pydantic import TypeAdapter, ValidationError

from ofw.diagnosis import TraceDiagnosis
from ofw.mine import TraceSnapshot

_SNAPSHOT_ADAPTER: TypeAdapter[TraceSnapshot] = TypeAdapter(TraceSnapshot)
_DIAGNOSIS_ADAPTER: TypeAdapter[TraceDiagnosis] = TypeAdapter(TraceDiagnosis)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    payload: str = sys.stdin.read()
    try:
        snapshot = _SNAPSHOT_ADAPTER.validate_json(payload)
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
    function: Callable[[TraceSnapshot], TraceDiagnosis] = cast(
        Callable[[TraceSnapshot], TraceDiagnosis],
        functions[0],
    )
    diagnosis: TraceDiagnosis = function(snapshot)
    sys.stdout.write(_DIAGNOSIS_ADAPTER.dump_json(diagnosis).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
