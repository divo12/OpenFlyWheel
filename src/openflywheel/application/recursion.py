"""Scoped recursion guard for background worker (no process env mutation)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_background_active: ContextVar[bool] = ContextVar("ofw_background_active", default=False)


def recursion_disabled() -> bool:
    return _background_active.get()


@contextmanager
def background_scope() -> Iterator[None]:
    token: Token[bool] = _background_active.set(True)
    try:
        yield
    finally:
        _background_active.reset(token)
