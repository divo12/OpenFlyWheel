"""Trusted transcript root and session reference tests."""

import json
from pathlib import Path

from openflywheel.connectors.agents.path_guard import (
    resolve_transcript_path,
    resolve_trusted_transcript_roots,
    validate_session_ref,
)
from openflywheel.connectors.agents.transcript import load_canonical_session
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import AgentSessionId


def test_cursor_transcript_under_dot_cursor_is_allowed(tmp_path: Path) -> None:
    agent_home = tmp_path / "cursor-home"
    project_root = tmp_path / "project"
    transcript_dir = agent_home / ".cursor" / "projects" / "abc"
    transcript_dir.mkdir(parents=True)
    project_root.mkdir()
    transcript = transcript_dir / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    roots = resolve_trusted_transcript_roots(
        agent_home=str(agent_home),
        project_root=str(project_root),
    )
    assert roots.error is None
    assert roots.data is not None
    validated = resolve_transcript_path(
        transcript_path=str(transcript.resolve()),
        allowed_roots=roots.data,
    )
    assert validated.error is None


def test_transcript_rejects_sensitive_dot_ssh(tmp_path: Path) -> None:
    agent_home = tmp_path / "home"
    project_root = tmp_path / "project"
    project_root.mkdir()
    ssh_dir = agent_home / ".ssh"
    ssh_dir.mkdir(parents=True)
    transcript = ssh_dir / "session.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    roots = resolve_trusted_transcript_roots(
        agent_home=str(agent_home),
        project_root=str(project_root),
    )
    assert roots.error is None
    assert roots.data is not None
    result = resolve_transcript_path(
        transcript_path=str(transcript.resolve()),
        allowed_roots=roots.data,
    )
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_SENSITIVE"


def test_session_ref_rejects_path_separators() -> None:
    result = validate_session_ref("../escape")
    assert result.error is not None
    assert result.error.code == "SESSION_REF_SEPARATOR"


def test_fixture_transcript_loads_with_trusted_roots(tmp_path: Path) -> None:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "agent-transcripts"
        / "claude-session.jsonl"
    )
    roots = resolve_trusted_transcript_roots(
        agent_home=str(fixture.parent.parent),
        project_root=str(fixture.parent.parent),
    )
    assert roots.error is None
    assert roots.data is not None
    loaded = load_canonical_session(
        platform=PlatformKind.CLAUDE_CODE,
        transcript_path=fixture,
        session_ref="sess-fixture",
        session_id=AgentSessionId("sess-fixture-id"),
        allowed_roots=roots.data,
    )
    assert loaded.error is None
    assert loaded.data is not None
    assert loaded.data.messages
