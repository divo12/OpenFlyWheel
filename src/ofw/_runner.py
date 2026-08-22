"""Child-process entrypoint for a file-backed Python lifecycle function."""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable
from typing import cast


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    module = importlib.import_module(sys.argv[1])
    functions = tuple(
        function
        for name, function in inspect.getmembers(module, inspect.isfunction)
        if name == sys.argv[2]
    )
    if len(functions) != 1:
        return 2
    function: Callable[[str], str] = cast(Callable[[str], str], functions[0])
    output: str = function(sys.stdin.read())  # type: ignore[misc]
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
