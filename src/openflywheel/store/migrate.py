"""Apply numbered SQL migrations atomically."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from openflywheel.store.db import validate_busy_timeout_ms
from openflywheel.store.migration_hook import MigrationStatementHook, NoOpMigrationStatementHook
from openflywheel.store.sqlite_access import fetch_one_row


def _migration_version(item: tuple[int, str]) -> int:
    return item[0]


def _migration_files() -> list[tuple[int, str]]:
    migrations_dir = resources.files("openflywheel.store").joinpath("migrations")
    files: list[tuple[int, str]] = []
    for entry in migrations_dir.iterdir():
        name = entry.name
        if not name.endswith(".sql"):
            continue
        version = int(name.split("_", maxsplit=1)[0])
        sql = entry.read_text(encoding="utf-8")
        files.append((version, sql))
    files.sort(key=_migration_version)
    return files


def _split_sql_statements(sql: str) -> tuple[str, ...]:
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped.upper().startswith("PRAGMA "):
            if stripped.upper().startswith("PRAGMA FOREIGN_KEYS"):
                statement = stripped.rstrip(";")
                statements.append(statement)
            buffer = []
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement.endswith(";"):
                statement = statement[:-1].strip()
            statements.append(statement)
            buffer = []
    if buffer:
        trailing = "\n".join(buffer).strip()
        if trailing:
            statements.append(trailing.rstrip(";"))
    return tuple(statements)


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = fetch_one_row(
        conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    )
    if row is None:
        return 0
    result = fetch_one_row(conn, "SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
    if result is None:
        return 0
    value = result[0]
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0


def _apply_pragmas(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    timeout = validate_busy_timeout_ms(busy_timeout_ms)
    conn.execute(f"PRAGMA busy_timeout = {int(timeout)}")


def _apply_one_migration(
    conn: sqlite3.Connection,
    *,
    version: int,
    sql: str,
    statement_hook: MigrationStatementHook,
    use_savepoint: bool,
) -> None:
    savepoint = f"migration_v{version}"
    if use_savepoint:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for statement in _split_sql_statements(sql):
            statement_hook.before_statement(version=version, statement=statement)
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(tz=UTC).isoformat()),
        )
        if use_savepoint:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        if use_savepoint:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _run_migration_with_optional_fk_disable(
    conn: sqlite3.Connection,
    *,
    version: int,
    sql: str,
    statement_hook: MigrationStatementHook,
    use_savepoint: bool,
) -> None:
    disable_fk = version == 4
    if disable_fk:
        conn.execute("PRAGMA foreign_keys=OFF")
    try:
        _apply_one_migration(
            conn,
            version=version,
            sql=sql,
            statement_hook=statement_hook,
            use_savepoint=use_savepoint,
        )
    finally:
        if disable_fk:
            conn.execute("PRAGMA foreign_keys=ON")


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = 5000,
    statement_hook: MigrationStatementHook | None = None,
    target_version: int | None = None,
) -> int:
    _apply_pragmas(conn, busy_timeout_ms)
    hook = statement_hook or NoOpMigrationStatementHook()
    applied = current_schema_version(conn)
    for version, sql in _migration_files():
        if version <= applied:
            continue
        if target_version is not None and version > target_version:
            break
        _run_migration_with_optional_fk_disable(
            conn,
            version=version,
            sql=sql,
            statement_hook=hook,
            use_savepoint=True,
        )
        applied = version
    return applied


def migrate_database(
    db_path: Path,
    *,
    busy_timeout_ms: int = 5000,
    statement_hook: MigrationStatementHook | None = None,
    target_version: int | None = None,
) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    hook = statement_hook or NoOpMigrationStatementHook()
    try:
        _apply_pragmas(conn, busy_timeout_ms=busy_timeout_ms)
        applied = current_schema_version(conn)
        for version, sql in _migration_files():
            if version <= applied:
                continue
            if target_version is not None and version > target_version:
                break
            if version == 4:
                conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                _apply_one_migration(
                    conn,
                    version=version,
                    sql=sql,
                    statement_hook=hook,
                    use_savepoint=False,
                )
                conn.execute("COMMIT")
                applied = version
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                if version == 4:
                    conn.execute("PRAGMA foreign_keys=ON")
        return applied
    finally:
        conn.isolation_level = previous_isolation
        conn.close()
