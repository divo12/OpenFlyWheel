"""SQLite connection factory and database wrapper."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MIN_BUSY_TIMEOUT_MS = 0
MAX_BUSY_TIMEOUT_MS = 300_000


def validate_busy_timeout_ms(value: int) -> int:
    if value < MIN_BUSY_TIMEOUT_MS or value > MAX_BUSY_TIMEOUT_MS:
        msg = f"busy_timeout_ms must be between {MIN_BUSY_TIMEOUT_MS} and {MAX_BUSY_TIMEOUT_MS}"
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path
    busy_timeout_ms: int = 5000
    ensure_parent: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "busy_timeout_ms", validate_busy_timeout_ms(self.busy_timeout_ms))


class ConnectionFactory:
    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config

    @property
    def path(self) -> Path:
        return self._config.path

    def connect(self) -> sqlite3.Connection:
        if self._config.ensure_parent:
            self._config.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._config.path))
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn)
        return conn

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        timeout_ms = validate_busy_timeout_ms(self._config.busy_timeout_ms)
        conn.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")


class Database:
    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    @property
    def path(self) -> Path:
        return self._factory.path

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Read-only connection; never commits."""
        conn = self._factory.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """All-or-nothing write transaction."""
        conn = self._factory.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Backward-compatible aliases
    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self.read() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.write() as conn:
            yield conn
