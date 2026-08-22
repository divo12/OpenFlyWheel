"""Langfuse connection and collection boundary contracts."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ofw import (
    CollectionError,
    CollectionErrorCode,
    Harness,
    LangfuseProject,
    LangfuseProjectMode,
    TraceWindow,
    ofw,
)


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _harness_root(tmp_path: Path) -> Path:
    root = tmp_path / "agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    return root


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

    assert project.mode is LangfuseProjectMode.EXISTING_PROJECT_READONLY
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


def test_observability_connection_changes_harness_revision_without_persisting_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _harness_root(tmp_path)
    baseline_harness = Harness("fixture-agent", root=root)
    baseline_harness.connect_prompt(ofw.editable(Path("prompt.md")))
    baseline = baseline_harness.process()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-sensitive")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-sensitive")
    project = LangfuseProject.from_env(
        environment="production",
        base_url="https://us.cloud.langfuse.com",
    )
    connected_harness = Harness("fixture-agent", root=root)
    connected_harness.connect_prompt(ofw.editable(Path("prompt.md")))
    connected_harness.connect_observability(project)

    connected = connected_harness.process()

    assert connected.id != baseline.id
    assert connected.observability is not None
    assert connected.observability == project.manifest()
    assert "pk-sensitive" not in connected.to_json()
    assert "sk-sensitive" not in connected.to_json()


def test_collect_requires_connected_observability(tmp_path: Path) -> None:
    root = _harness_root(tmp_path)
    harness = Harness("fixture-agent", root=root)
    harness.connect_prompt(ofw.editable(Path("prompt.md")))
    revision = harness.process()
    start = datetime(2026, 8, 22, tzinfo=UTC)

    with pytest.raises(CollectionError) as raised:
        ofw.collect(
            revision,
            window=TraceWindow(start=start, end=start + timedelta(hours=1)),
        )

    assert raised.value.code is CollectionErrorCode.OBSERVABILITY_NOT_CONNECTED
