"""Typed accessors for sqlite3.Row cells."""

from __future__ import annotations

import sqlite3


def fetch_one_row(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...] = (),
) -> sqlite3.Row | None:
    cursor = conn.execute(sql, params)
    return cursor.fetchone()


def fetch_all_rows(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...] = (),
) -> tuple[sqlite3.Row, ...]:
    cursor = conn.execute(sql, params)
    return tuple(cursor.fetchall())


def cell_str(row: sqlite3.Row, column: str) -> str:
    value = row[column]
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, int | float):
        return str(value)
    msg = f"Expected textual sqlite cell for {column}"
    raise TypeError(msg)


def cell_optional_str(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    msg = f"Expected optional textual sqlite cell for {column}"
    raise TypeError(msg)


def cell_int(row: sqlite3.Row, column: str) -> int:
    value = row[column]
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    msg = f"Expected integer sqlite cell for {column}"
    raise TypeError(msg)
