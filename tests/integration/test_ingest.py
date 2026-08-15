"""Episode ingest integration tests."""

from datetime import UTC, datetime
from pathlib import Path

from tests.helpers import onboard_and_lock

from openflywheel.application.ingest_app import IngestApplication
from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.connectors.github.fixture import FixtureGitHubClient
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.enums import LocatorKind, RejectReason, SourceKind, VisibilityLevel
from openflywheel.contracts.episode import SourceReference
from openflywheel.contracts.evidence import EvidenceLocator
from openflywheel.ingest.admission import compute_checksum, evaluate_admission
from openflywheel.store.checkpoint_hook import AbortCheckpointCommitHook
from openflywheel.store.repos.audit_repo import SqliteAuditRejectRepository
from openflywheel.store.repos.episode_repo import SqliteEpisodeRepository
from openflywheel.store.repos.source_repo import SqliteSourceRepository
from openflywheel.store.uow import IngestUnitOfWork


def test_admission_rejects_secrets(fixture_root: Path) -> None:
    fake_aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
    envelope = ConnectorEnvelope(
        external_id="repo-beta/secrets/fake.env",
        uri="fixture://repo-beta/secrets/fake.env",
        content_text=fake_aws_key,
        content_type="text/plain",
        event_time=datetime.now(tz=UTC),
        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
    )
    verdict = evaluate_admission(envelope, excluded_paths=tuple())
    assert verdict.decision.value == "reject"
    assert verdict.reason == RejectReason.LIKELY_SECRET


def test_admission_rejects_excluded_paths(fixture_root: Path) -> None:
    envelope = ConnectorEnvelope(
        external_id="repo-beta/secrets/fake.env",
        uri="fixture://repo-beta/secrets/fake.env",
        content_text="harmless",
        content_type="text/plain",
        event_time=datetime.now(tz=UTC),
        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
    )
    verdict = evaluate_admission(envelope, excluded_paths=("secrets/",))
    assert verdict.decision.value == "reject"
    assert verdict.reason == RejectReason.EXCLUDED_PATH


def test_locked_exclusions_apply_without_cli_exclude(
    workspace_home: Path, fixture_root: Path
) -> None:
    onboard_and_lock(workspace_home, fixture_root, beta_exclusions=("secrets/",))
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    app = IngestApplication(database)
    result = app.run_fixture_ingest(
        workspace_id=config.workspace_id,
        fixture_root=fixture_root,
        cli_excluded_paths=tuple(),
    )
    assert result.error is None

    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, config.workspace_id, SourceKind.GITHUB.value
        )
        assert source is not None
        episodes = SqliteEpisodeRepository().list_episodes_for_source(conn, source.id)
        all_text = "\n".join(ep.content_text for ep in episodes)
        stored_paths = {ep.source_ref.external_id for ep in episodes}

    assert "repo-beta/secrets/fake.env" not in stored_paths
    assert "AKIA" not in all_text


def test_ingest_idempotent_episode_ids(workspace_home: Path, fixture_root: Path) -> None:
    onboard_and_lock(workspace_home, fixture_root, beta_exclusions=("secrets/",))
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    app = IngestApplication(database)

    first = app.run_fixture_ingest(
        workspace_id=config.workspace_id,
        fixture_root=fixture_root,
    )
    second = app.run_fixture_ingest(
        workspace_id=config.workspace_id,
        fixture_root=fixture_root,
    )
    assert first.error is None and second.error is None
    assert first.data is not None and second.data is not None
    assert first.data.episode_ids == second.data.episode_ids
    assert first.data.accepted_count > 0


def test_idempotency_same_bytes_different_paths(workspace_home: Path, fixture_root: Path) -> None:
    onboard_and_lock(workspace_home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)

    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, config.workspace_id, SourceKind.GITHUB.value
        )
        assert source is not None
        source_id = source.id

    content = "shared-content"
    checksum = compute_checksum(content)
    uow = IngestUnitOfWork(database)
    first = uow.commit_episode_bundle(
        workspace_id=config.workspace_id,
        source_id=source_id,
        source_ref=SourceReference(source_id=source_id, external_id="a.txt", uri="fixture://a.txt"),
        content_text=content,
        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
        event_time=datetime.now(tz=UTC),
        ingest_time=datetime.now(tz=UTC),
        checksum=checksum,
        content_type="text/plain",
        anchors=((EvidenceLocator(kind=LocatorKind.FILE_LINE, value="a.txt:1"), "a"),),
        checkpoint_cursor="a.txt",
    )
    second = uow.commit_episode_bundle(
        workspace_id=config.workspace_id,
        source_id=source_id,
        source_ref=SourceReference(source_id=source_id, external_id="b.txt", uri="fixture://b.txt"),
        content_text=content,
        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
        event_time=datetime.now(tz=UTC),
        ingest_time=datetime.now(tz=UTC),
        checksum=checksum,
        content_type="text/plain",
        anchors=((EvidenceLocator(kind=LocatorKind.FILE_LINE, value="b.txt:1"), "b"),),
        checkpoint_cursor="b.txt",
    )
    assert first.episode.id != second.episode.id


def test_idempotency_same_path_new_hash_creates_new_episode(
    workspace_home: Path, fixture_root: Path
) -> None:
    onboard_and_lock(workspace_home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)

    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, config.workspace_id, SourceKind.GITHUB.value
        )
        assert source is not None
        source_id = source.id

    uow = IngestUnitOfWork(database)
    first = uow.commit_episode_bundle(
        workspace_id=config.workspace_id,
        source_id=source_id,
        source_ref=SourceReference(source_id=source_id, external_id="c.txt", uri="fixture://c.txt"),
        content_text="v1",
        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
        event_time=datetime.now(tz=UTC),
        ingest_time=datetime.now(tz=UTC),
        checksum=compute_checksum("v1"),
        content_type="text/plain",
        anchors=((EvidenceLocator(kind=LocatorKind.FILE_LINE, value="c.txt:1"), "c"),),
        checkpoint_cursor="c.txt",
    )
    second = uow.commit_episode_bundle(
        workspace_id=config.workspace_id,
        source_id=source_id,
        source_ref=SourceReference(source_id=source_id, external_id="c.txt", uri="fixture://c.txt"),
        content_text="v2",
        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
        event_time=datetime.now(tz=UTC),
        ingest_time=datetime.now(tz=UTC),
        checksum=compute_checksum("v2"),
        content_type="text/plain",
        anchors=((EvidenceLocator(kind=LocatorKind.FILE_LINE, value="c.txt:1"), "c"),),
        checkpoint_cursor="c.txt",
    )
    assert first.episode.id != second.episode.id


def test_binary_file_audited_as_unsupported(workspace_home: Path, fixture_root: Path) -> None:
    onboard_and_lock(workspace_home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    app = IngestApplication(database)
    result = app.run_fixture_ingest(
        workspace_id=config.workspace_id,
        fixture_root=fixture_root,
    )
    assert result.error is None

    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, config.workspace_id, SourceKind.GITHUB.value
        )
        assert source is not None
        rejects = SqliteAuditRejectRepository().list_rejects_for_source(conn, source.id)
        episodes = SqliteEpisodeRepository().list_episodes_for_source(conn, source.id)

    assert any(r.external_id == "repo-beta/binary.dat" for r in rejects)
    assert "repo-beta/binary.dat" not in {ep.source_ref.external_id for ep in episodes}


def test_duplicate_audit_rejects_not_created_on_reingest(
    workspace_home: Path, fixture_root: Path
) -> None:
    onboard_and_lock(workspace_home, fixture_root, beta_exclusions=("secrets/",))
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    app = IngestApplication(database)

    first = app.run_fixture_ingest(workspace_id=config.workspace_id, fixture_root=fixture_root)
    second = app.run_fixture_ingest(workspace_id=config.workspace_id, fixture_root=fixture_root)
    assert first.error is None and second.error is None

    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, config.workspace_id, SourceKind.GITHUB.value
        )
        assert source is not None
        rejects = SqliteAuditRejectRepository().list_rejects_for_source(conn, source.id)

    secret_rejects = [r for r in rejects if r.external_id == "repo-beta/secrets/fake.env"]
    binary_rejects = [r for r in rejects if r.external_id == "repo-beta/binary.dat"]
    assert len(secret_rejects) == 1
    assert len(binary_rejects) == 1


def test_checkpoint_rollback_leaves_store_unchanged(
    workspace_home: Path, fixture_root: Path
) -> None:
    onboard_and_lock(workspace_home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)

    with database.read() as conn:
        source = SqliteSourceRepository().get_by_slug(
            conn, config.workspace_id, SourceKind.GITHUB.value
        )
        assert source is not None
        source_id = source.id

    uow = IngestUnitOfWork(database)
    before_checkpoint = uow.read_checkpoint(source_id)
    before_episodes = uow.count_episodes(source_id)
    before_anchors = uow.count_anchors_for_source(source_id)

    client = FixtureGitHubClient(fixture_root)
    envelope = client.list_file_envelopes()[0]
    failing_uow = IngestUnitOfWork(database, checkpoint_hook=AbortCheckpointCommitHook())
    from openflywheel.store.exceptions import IngestTransactionError

    try:
        failing_uow.commit_episode_bundle(
            workspace_id=config.workspace_id,
            source_id=source_id,
            source_ref=SourceReference(
                source_id=source_id,
                external_id=envelope.external_id,
                uri=envelope.uri,
            ),
            content_text=envelope.content_text,
            acl=envelope.acl,
            event_time=envelope.event_time,
            ingest_time=datetime.now(tz=UTC),
            checksum=compute_checksum(envelope.content_text) + "-unique",
            content_type=envelope.content_type,
            anchors=((EvidenceLocator(kind=LocatorKind.FILE_LINE, value="x:1"), "x"),),
            checkpoint_cursor=envelope.external_id,
        )
        raised = False
    except IngestTransactionError:
        raised = True

    assert raised
    assert uow.read_checkpoint(source_id) == before_checkpoint
    assert uow.count_episodes(source_id) == before_episodes
    assert uow.count_anchors_for_source(source_id) == before_anchors
