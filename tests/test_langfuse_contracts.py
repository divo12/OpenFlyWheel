"""Langfuse connection and collection boundary contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ofw import (
    CollectionError,
    CollectionErrorCode,
    LangfuseProject,
    TraceWindow,
)


def test_from_env_keeps_credentials_out_of_manifest_and_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-sensitive")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-sensitive")
    project = LangfuseProject.from_env(
        environment="production",
        base_url="https://us.cloud.langfuse.com",
    )

    manifest = project.manifest()
    rendered = manifest.to_json()

    assert manifest.environment.value == "production"
    assert manifest.public_key_environment.value == "LANGFUSE_PUBLIC_KEY"
    assert manifest.secret_key_environment.value == "LANGFUSE_SECRET_KEY"
    assert "pk-sensitive" not in rendered
    assert "sk-sensitive" not in rendered
    assert "pk-sensitive" not in repr(project)
    assert "sk-sensitive" not in repr(project)


@pytest.mark.parametrize(
    "base_url",
    (
        "http://langfuse.example.com",
        "https://user:password@langfuse.example.com",
        "https://langfuse.example.com/path",
        "https://langfuse.example.com?token=secret",
        "https://127.0.0.1:3000",
    ),
)
def test_unsafe_base_url_fails(base_url: str) -> None:
    with pytest.raises(CollectionError) as raised:
        LangfuseProject.from_env(environment="production", base_url=base_url)
    assert raised.value.code is CollectionErrorCode.UNSAFE_HOST


def test_explicit_local_development_host_is_allowed() -> None:
    project = LangfuseProject.from_env(
        environment="development",
        base_url="http://127.0.0.1:3000",
        allow_private_network=True,
    )
    assert project.manifest().base_url.value == "http://127.0.0.1:3000"


def test_trace_window_requires_aware_utc_ordering() -> None:
    start = datetime(2026, 8, 22, tzinfo=UTC)
    valid = TraceWindow(start=start, end=start + timedelta(hours=1))
    assert valid.start.tzinfo is UTC
    zoneinfo_utc = TraceWindow(
        start=datetime(2026, 8, 22, tzinfo=ZoneInfo("UTC")),
        end=datetime(2026, 8, 22, 1, tzinfo=ZoneInfo("UTC")),
    )
    assert zoneinfo_utc.start.utcoffset() == timedelta(0)
    with pytest.raises(CollectionError) as naive:
        TraceWindow(start=datetime(2026, 8, 22), end=datetime(2026, 8, 23))
    with pytest.raises(CollectionError) as reversed_window:
        TraceWindow(start=start, end=start)

    assert naive.value.code is CollectionErrorCode.INVALID_WINDOW
    assert reversed_window.value.code is CollectionErrorCode.INVALID_WINDOW
