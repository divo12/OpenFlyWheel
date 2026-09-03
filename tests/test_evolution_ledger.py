from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ofw.evolution.ledger import (
    EvolutionEvent,
    EvolutionEventDraft,
    EvolutionEventType,
    EvolutionLedgerErrorCode,
    EvolutionLedgerFailure,
    EvolutionStarted,
    FileEvolutionLedger,
    HypothesisLinked,
    ReleasePublished,
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file").write_text("x", encoding="utf-8")
    import subprocess

    subprocess.run(("git", "-C", str(root), "init", "-q"), check=True)
    return root


def _draft(experiment_id: str = "experiment-one") -> EvolutionEventDraft:
    return EvolutionEventDraft(
        event_type=EvolutionEventType.EVOLUTION_STARTED,
        experiment_id=experiment_id,
        payload=EvolutionStarted(policy_digest="sha256:" + "a" * 64),
        occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
        correlation_id="request-1",
    )


def test_append_is_fsynced_typed_and_keyset_paginated(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()

    first = ledger.append(root, _draft())
    second = ledger.append(
        root,
        EvolutionEventDraft(
            event_type=EvolutionEventType.EVOLUTION_STARTED,
            experiment_id="experiment-one",
            payload=EvolutionStarted(policy_digest="sha256:" + "b" * 64),
            occurred_at=datetime(2026, 9, 3, 0, 0, 1, tzinfo=UTC),
            correlation_id="request-2",
        ),
    )
    assert (first.sequence, second.sequence) == (1, 2)
    page = ledger.page(root, "experiment-one", limit=1)
    assert page.events == (first,)
    assert page.next_cursor is not None
    assert ledger.page(
        root, "experiment-one", cursor=page.next_cursor, limit=1
    ).events == (second,)


def test_empty_log_and_invalid_draft_inputs_are_handled(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    assert ledger.page(root, "experiment-one").events == ()
    with pytest.raises(EvolutionLedgerFailure):
        EvolutionEventDraft(
            event_type=EvolutionEventType.EVOLUTION_STARTED,
            experiment_id="bad/id",
            payload=EvolutionStarted(policy_digest="sha256:" + "a" * 64),
            occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
        )
    with pytest.raises(EvolutionLedgerFailure):
        EvolutionEventDraft(
            event_type=EvolutionEventType.EVOLUTION_STARTED,
            experiment_id="experiment-one",
            payload=EvolutionStarted(policy_digest="sha256:" + "a" * 64),
            occurred_at=datetime(2026, 9, 3),
        )
    with pytest.raises(ValidationError):
        EvolutionEvent(
            experiment_id="experiment-one",
            sequence=1,
            event_id="sha256:" + "a" * 64,
            event_type=EvolutionEventType.EVOLUTION_STARTED,
            occurred_at=datetime(2026, 9, 3),
            payload_digest="sha256:" + "a" * 64,
            payload=EvolutionStarted(policy_digest="sha256:" + "a" * 64),
        )


def test_identical_retry_is_idempotent_and_conflict_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    first = ledger.append(root, _draft())
    assert ledger.append(root, _draft()) == first

    with pytest.raises(EvolutionLedgerFailure) as raised:
        ledger.append(
            root,
            EvolutionEventDraft(
                event_type=EvolutionEventType.EVOLUTION_STARTED,
                experiment_id="experiment-one",
                payload=EvolutionStarted(policy_digest="sha256:" + "c" * 64),
                occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
                correlation_id="request-1",
            ),
        )
    assert raised.value.code is EvolutionLedgerErrorCode.EVENT_CONFLICT


def test_corrupt_tail_fails_closed_with_last_valid_sequence(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    ledger.append(root, _draft())
    path = root / ".git" / "ofw" / "preparations" / "experiment-one" / "evolution.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"schema_version":1')
    with pytest.raises(EvolutionLedgerFailure) as raised:
        ledger.page(root, "experiment-one")
    assert raised.value.code is EvolutionLedgerErrorCode.CORRUPT_LEDGER
    assert raised.value.last_valid_sequence == 1


def test_gapped_sequence_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    ledger.append(root, _draft())
    path = root / ".git" / "ofw" / "preparations" / "experiment-one" / "evolution.jsonl"
    content = path.read_text(encoding="utf-8").replace('"sequence":1', '"sequence":3')
    path.write_text(content, encoding="utf-8")
    with pytest.raises(EvolutionLedgerFailure) as raised:
        ledger.page(root, "experiment-one")
    assert raised.value.code is EvolutionLedgerErrorCode.SEQUENCE_GAP
    assert raised.value.last_valid_sequence == 0


def test_cursor_and_count_bounds_are_typed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    ledger.append(root, _draft())
    with pytest.raises(EvolutionLedgerFailure) as raised:
        ledger.page(root, "experiment-one", limit=0)
    assert raised.value.code is EvolutionLedgerErrorCode.INVALID_EVENT
    with pytest.raises(EvolutionLedgerFailure) as raised:
        ledger.page(root, "experiment-one", cursor="not-a-cursor")
    assert raised.value.code is EvolutionLedgerErrorCode.CURSOR_INVALID


def test_invalid_workspace_and_payload_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(EvolutionLedgerFailure) as raised:
        FileEvolutionLedger().page(tmp_path / "missing", "experiment-one")
    assert raised.value.code is EvolutionLedgerErrorCode.INVALID_WORKSPACE
    with pytest.raises(ValidationError):
        EvolutionEvent(
            experiment_id="experiment-one",
            sequence=1,
            event_id="sha256:" + "a" * 64,
            event_type=EvolutionEventType.EVOLUTION_STARTED,
            occurred_at=datetime(2026, 9, 3, tzinfo=UTC),
            payload_digest="sha256:" + "a" * 64,
            payload=HypothesisLinked(
                hypothesis_id="sha256:" + "a" * 64,
                source_commit="a" * 40,
            ),
        )


def test_legacy_release_payloads_remain_readable_after_publication_fields_are_added(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    ledger.append(root, _draft())
    payload_json = ReleasePublished(
        release_id="legacy",
        content_commit="a" * 40,
        content_id=None,
        target_reached=False,
    ).model_dump_json(
        exclude={
            "content_tree",
            "parent_release_id",
            "expected_current_commit",
            "policy_digest",
            "operation_id",
            "intent_event_id",
        }
    )
    identity = "\0".join(("experiment-one", "ReleasePublished", "old", "old", ""))
    event_json = (
        '{"schema_version":1,"experiment_id":"experiment-one","sequence":2,'
        '"event_id":"sha256:'
        + hashlib.sha256(identity.encode()).hexdigest()
        + '","event_type":"ReleasePublished","occurred_at":"2026-09-03T00:00:00Z",'
        '"causation_id":"old","correlation_id":"old","request_digest":null,'
        '"payload_digest":"sha256:'
        + hashlib.sha256(payload_json.encode()).hexdigest()
        + '","payload":'
        + payload_json
        + "}"
    )
    path = root / ".git/ofw/preparations/experiment-one/evolution.jsonl"
    with path.open("ab") as stream:
        stream.write((event_json + "\n").encode())

    assert isinstance(ledger.events(root, "experiment-one")[-1].payload, ReleasePublished)


def test_writer_owner_lock_is_exclusive(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    lock = root / ".git" / "ofw" / "preparations" / "experiment-one" / ".evolution.lock"
    lock.parent.mkdir(parents=True)
    lock.mkdir()
    with pytest.raises(EvolutionLedgerFailure) as raised:
        ledger.append(root, _draft("experiment-one"))
    assert raised.value.code is EvolutionLedgerErrorCode.BUSY
    lock.rmdir()


def test_append_rejects_non_regular_log_and_oversized_log(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    ledger = FileEvolutionLedger()
    control = root / ".git" / "ofw" / "preparations" / "experiment-one"
    control.mkdir(parents=True)
    (control / "evolution.jsonl").mkdir()
    with pytest.raises(EvolutionLedgerFailure) as raised:
        ledger.append(root, _draft())
    assert raised.value.code is EvolutionLedgerErrorCode.INVALID_WORKSPACE

    (control / "evolution.jsonl").rmdir()
    (control / "evolution.jsonl").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(EvolutionLedgerFailure) as raised:
        ledger.page(root, "experiment-one")
    assert raised.value.code is EvolutionLedgerErrorCode.LEDGER_TOO_LARGE
