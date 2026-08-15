"""Platform CLI alias tests."""

import pytest

from openflywheel.contracts.enums import PlatformKind, parse_platform_kind


def test_parse_platform_kind_accepts_claude_code_alias() -> None:
    assert parse_platform_kind("claude-code") == PlatformKind.CLAUDE_CODE
    assert parse_platform_kind("claude_code") == PlatformKind.CLAUDE_CODE


def test_parse_platform_kind_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unsupported platform"):
        parse_platform_kind("unknown-platform")
