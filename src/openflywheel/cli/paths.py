"""Resolve workspace home paths consistently."""

from __future__ import annotations

from pathlib import Path


def resolve_home(home: str) -> Path:
    return Path(home).expanduser().resolve()
