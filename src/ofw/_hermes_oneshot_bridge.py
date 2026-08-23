"""Run Hermes from a private stdin prompt and an isolated configuration."""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast


class _RunOneshot(Protocol):
    def __call__(
        self,
        prompt: str,
        model: str | None = None,
        provider: str | None = None,
        toolsets: str | None = None,
    ) -> int: ...


class _ReasoningLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


def main() -> int:
    if len(sys.argv) != 5:
        return 2
    try:
        provider = _required(sys.argv[1])
        model = _required(sys.argv[2])
        reasoning = _ReasoningLevel(sys.argv[3])
        expected_version = _required(sys.argv[4])
        prompt_payload: str = sys.stdin.read()
        prompt = _required(prompt_payload)
        _isolate_hermes(reasoning)
        from hermes_cli import __version__ as version_value  # type: ignore[import-not-found]
        from hermes_cli.oneshot import run_oneshot as run_value  # type: ignore[import-not-found]
    except (ImportError, OSError, ValueError):
        return 2
    version = cast(str, version_value)
    run_oneshot = cast(_RunOneshot, run_value)
    if version != expected_version:
        return 2
    result: int = run_oneshot(
        prompt,
        model=model,
        provider=provider,
        toolsets="context_engine",
    )
    return result


def _isolate_hermes(reasoning: _ReasoningLevel) -> None:
    home = Path.cwd().resolve(strict=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_SAFE_MODE"] = "1"
    os.environ["HERMES_IGNORE_USER_CONFIG"] = "1"
    os.environ["HERMES_IGNORE_RULES"] = "1"
    (home / "config.yaml").write_text(
        "agent:\n"
        f"  reasoning_effort: {reasoning.value}\n"
        "context:\n"
        "  engine: compressor\n"
        "memory:\n"
        "  memory_enabled: false\n"
        "  user_profile_enabled: false\n",
        encoding="utf-8",
    )


def _required(value: str) -> str:
    selected = value.strip()
    if not selected:
        raise ValueError("value is required")
    return selected


if __name__ == "__main__":
    raise SystemExit(main())
