"""Database configuration validation tests."""

from __future__ import annotations

import pytest

from openflywheel.store.db import (
    MAX_BUSY_TIMEOUT_MS,
    MIN_BUSY_TIMEOUT_MS,
    DatabaseConfig,
    validate_busy_timeout_ms,
)


def test_validate_busy_timeout_accepts_range() -> None:
    assert validate_busy_timeout_ms(MIN_BUSY_TIMEOUT_MS) == MIN_BUSY_TIMEOUT_MS
    assert validate_busy_timeout_ms(MAX_BUSY_TIMEOUT_MS) == MAX_BUSY_TIMEOUT_MS
    assert validate_busy_timeout_ms(5000) == 5000


def test_validate_busy_timeout_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        validate_busy_timeout_ms(-1)
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        validate_busy_timeout_ms(MAX_BUSY_TIMEOUT_MS + 1)


def test_database_config_validates_busy_timeout() -> None:
    from pathlib import Path

    config = DatabaseConfig(path=Path("/tmp/book.sqlite"), busy_timeout_ms=1000)
    assert config.busy_timeout_ms == 1000
