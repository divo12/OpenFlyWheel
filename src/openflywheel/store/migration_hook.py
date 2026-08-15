"""Test-only hooks for migration failure injection."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MigrationStatementHook(Protocol):
    def before_statement(self, *, version: int, statement: str) -> None:
        """Raise to abort migration before executing the statement."""


class NoOpMigrationStatementHook:
    def before_statement(self, *, version: int, statement: str) -> None:
        return None


class AbortMigrationStatementHook:
    def __init__(self, *, version: int, statement_contains: str) -> None:
        self._version = version
        self._needle = statement_contains

    def before_statement(self, *, version: int, statement: str) -> None:
        if version == self._version and self._needle in statement:
            msg = f"Injected migration abort at version {version}"
            raise RuntimeError(msg)
