"""Claude transcript projection tests."""

from pathlib import Path

from openflywheel.connectors.agents.claude_transcript import load_claude_transcript
from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.ids import AgentSessionId


def test_claude_transcript_filters_tool_and_system_events() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "agent-transcripts"
        / "claude-session.jsonl"
    )
    session = load_claude_transcript(
        path,
        session_ref="demo-session",
        session_id=AgentSessionId("sess-1"),
    )
    assert session.platform == PlatformKind.CLAUDE_CODE
    assert len(session.messages) == 2
    assert session.messages[0].role == "user"
    assert "memory gated" in session.messages[0].text
    assert session.messages[1].role == "assistant"
    assert "should gate memory" in session.messages[1].text


def test_claude_transcript_empty_file_yields_no_messages(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    session = load_claude_transcript(
        path,
        session_ref="empty",
        session_id=AgentSessionId("sess-2"),
    )
    assert session.messages == ()
