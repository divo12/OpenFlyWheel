"""Transcript path and malformed JSON safety tests."""

import json
from pathlib import Path

from openflywheel.connectors.agents.path_guard import resolve_transcript_path
from openflywheel.connectors.agents.transcript import load_canonical_session
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import AgentSessionId


def test_transcript_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    result = resolve_transcript_path(
        transcript_path=str(root / ".." / "outside.jsonl"),
        allowed_roots=(root,),
    )
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_TRAVERSAL"


def test_transcript_rejects_outside_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    transcript = outside / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')
    result = resolve_transcript_path(
        transcript_path=str(transcript.resolve()),
        allowed_roots=(allowed,),
    )
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_OUTSIDE_ROOT"


def test_malformed_json_returns_typed_failure(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    bad = root / "bad.jsonl"
    bad.write_text("{not json\n", encoding="utf-8")
    result = load_canonical_session(
        platform=PlatformKind.CLAUDE_CODE,
        transcript_path=bad,
        session_ref="sess-bad",
        session_id=AgentSessionId("sess-bad-id"),
        allowed_roots=(root,),
    )
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_MALFORMED"


def test_transcript_rejects_sensitive_hidden_dir(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    ssh_dir = root / ".ssh"
    root.mkdir()
    ssh_dir.mkdir()
    transcript = ssh_dir / "session.jsonl"
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
    result = resolve_transcript_path(
        transcript_path=str(transcript.resolve()),
        allowed_roots=(root,),
    )
    assert result.error is not None
    assert result.error.code == "TRANSCRIPT_SENSITIVE"
