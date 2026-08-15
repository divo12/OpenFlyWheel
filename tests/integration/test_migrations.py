"""SQLite migration and pragma tests."""

import sqlite3
from pathlib import Path

import pytest

from openflywheel.store.db import ConnectionFactory, Database, DatabaseConfig
from openflywheel.store.migrate import apply_migrations, migrate_database


def test_migrations_create_foundation_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "book.sqlite"
    factory = ConnectionFactory(DatabaseConfig(path=db_path))
    with factory.connect() as conn:
        version = apply_migrations(conn)
        conn.commit()
    assert version == 5

    conn = sqlite3.connect(str(db_path))
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    assert "workspaces" in tables
    assert "episodes" in tables
    assert "audit_rejects" in tables
    assert "checkpoints" in tables


def test_migration_version_recorded_atomically(tmp_path: Path) -> None:
    db_path = tmp_path / "book.sqlite"
    version = migrate_database(db_path)
    assert version == 5

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    conn.close()
    assert [int(row[0]) for row in rows] == [1, 2, 3, 4, 5]


def test_failed_migration_rolls_back_without_partial_version(tmp_path: Path) -> None:
    from openflywheel.store.migration_hook import AbortMigrationStatementHook

    db_path = tmp_path / "book.sqlite"
    hook = AbortMigrationStatementHook(
        version=2,
        statement_contains="idx_episodes_source_external_checksum",
    )
    with pytest.raises(RuntimeError, match="Injected migration abort"):
        migrate_database(db_path, statement_hook=hook)

    conn = sqlite3.connect(str(db_path))
    version_rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    index_row = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='index' AND name='idx_episodes_source_external_checksum'
        """
    ).fetchone()
    conn.close()
    assert [int(row[0]) for row in version_rows] == [1]
    assert index_row is None


def test_wal_and_foreign_keys_enabled(tmp_path: Path) -> None:
    db_path = tmp_path / "book.sqlite"
    database = Database(ConnectionFactory(DatabaseConfig(path=db_path)))
    with database.write() as conn:
        apply_migrations(conn)
        journal = conn.execute("PRAGMA journal_mode").fetchone()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()
    assert journal is not None and journal[0] == "wal"
    assert fk is not None and int(fk[0]) == 1


def test_foreign_key_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "book.sqlite"
    factory = ConnectionFactory(DatabaseConfig(path=db_path))
    with factory.connect() as conn:
        apply_migrations(conn)
        raised = False
        try:
            conn.execute(
                """
                INSERT INTO episodes
                (id, workspace_id, source_id, external_id, uri, content_text, acl_json,
                 event_time, ingest_time, checksum, content_type)
                VALUES (
                    'e1', 'missing-ws', 'missing-src', 'x', 'u', 'c', '{}',
                    't', 't', 'h', 'text/plain'
                )
                """
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raised = True
    assert raised


def test_book_hardening_triggers_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "book.sqlite"
    migrate_database(db_path)
    conn = sqlite3.connect(str(db_path))
    triggers = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    conn.close()
    assert "claim_fts_ai" in triggers
    assert "claim_fts_ad" in triggers
    assert "claim_fts_au" in triggers
    assert "pins_no_update" in triggers
    assert "pins_no_delete" in triggers
    assert "pin_claim_snapshots_no_update" in triggers
    assert "pin_claim_snapshots_no_delete" in triggers
    assert "pin_anchor_snapshots_no_update" in triggers
    assert "pin_anchor_snapshots_no_delete" in triggers
    assert "pin_edge_snapshots_no_update" in triggers
    assert "pin_edge_snapshots_no_delete" in triggers
    assert "idx_coverage_boundary_slot" in indexes


def test_episode_idempotency_index_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "book.sqlite"
    migrate_database(db_path)
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='index' AND name='idx_episodes_source_external_checksum'
        """
    ).fetchone()
    conn.close()
    assert row is not None


def test_pin_anchor_snapshot_composite_primary_key(tmp_path: Path) -> None:
    db_path = tmp_path / "book.sqlite"
    migrate_database(db_path)
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='pin_anchor_snapshots'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert "PRIMARY KEY (pin_id, claim_id, anchor_id)" in str(row[0])


def test_migration_v4_upgrades_populated_v3_with_claim_fk(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from openflywheel.contracts.acl import AclLabel
    from openflywheel.contracts.boundary import BoundaryManifest, SourceAuthorityRule
    from openflywheel.contracts.enums import (
        ClaimState,
        DeploymentMode,
        IdentityKind,
        ProposalStatus,
        SystemShape,
        TruthSection,
        VisibilityLevel,
    )
    from openflywheel.contracts.ids import BoundaryId, ClaimId, ProposalId, WorkspaceId
    from openflywheel.contracts.workspace import WorkspacePolicy
    from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
    from openflywheel.store.repos.claim_repo import SqliteClaimRepository
    from openflywheel.store.repos.proposal_repo import SqliteProposalRepository
    from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

    db_path = tmp_path / "book.sqlite"
    migrate_database(db_path, target_version=3)
    database = Database(ConnectionFactory(DatabaseConfig(path=db_path)))
    now = datetime.now(tz=UTC)
    proposal_id = ProposalId("prop-upgrade-1")
    claim_id = ClaimId("claim-upgrade-1")
    boundary_id = BoundaryId("boundary-upgrade-1")

    with database.write() as conn:
        ws_repo = SqliteWorkspaceRepository()
        workspace = ws_repo.create_workspace(
            conn,
            name="UpgradeCo",
            deployment_mode=DeploymentMode.LOCAL,
            policy=WorkspacePolicy(default_visibility=VisibilityLevel.INTERNAL),
            admin_identity_ids=tuple(),
            created_at=now,
            workspace_id=WorkspaceId("ws-upgrade-1"),
        )
        owner = ws_repo.create_identity(
            conn,
            workspace_id=workspace.id,
            kind=IdentityKind.HUMAN,
            display_name="Owner",
            created_at=now,
        )
        manifest = BoundaryManifest(
            version=1,
            purpose="Upgrade probe boundary",
            system_shape=SystemShape.MULTI_REPO,
            owner_identity_ids=(owner.id,),
            primary_kpi="U3",
            source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
            exclusions=tuple(),
            locked_at=now,
        )
        boundaries = SqliteBoundaryRepository()
        boundaries.upsert_boundary(
            conn,
            workspace_id=workspace.id,
            name="Alpha",
            slug="repo-alpha",
            component_paths=("src/",),
            manifest=manifest,
            created_at=now,
            boundary_id=boundary_id,
        )
        proposals = SqliteProposalRepository()
        proposals.insert_proposal(
            conn,
            workspace_id=workspace.id,
            boundary_id=boundary_id,
            what="Package name is alphacore",
            how="Declared in pyproject.toml",
            section=TruthSection.U3,
            proposer_identity_id=owner.id,
            anchor_ids=tuple(),
            status=ProposalStatus.PENDING,
            idempotency_key="upgrade-probe",
            created_at=now,
            proposal_id=proposal_id,
        )
        claims = SqliteClaimRepository()
        claims.insert_claim(
            conn,
            workspace_id=workspace.id,
            boundary_id=boundary_id,
            what="Package name is alphacore",
            how="Declared in pyproject.toml",
            section=TruthSection.U3,
            state=ClaimState.ACTIVE,
            authority_identity_id=owner.id,
            acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
            valid_from=now,
            valid_to=None,
            source_proposal_id=proposal_id,
            claim_id=claim_id,
        )

    version = migrate_database(db_path)
    assert version == 5

    with database.read() as conn:
        fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_issues == []
        loaded_proposal = SqliteProposalRepository().get_proposal(conn, proposal_id)
        assert loaded_proposal is not None
        assert loaded_proposal.boundary_id == boundary_id
        loaded_claim = SqliteClaimRepository().get_claim(conn, claim_id)
        assert loaded_claim is not None
        assert loaded_claim.source_proposal_id == proposal_id


def test_migration_v5_dedupes_agent_sessions_keeps_earliest(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from openflywheel.contracts.enums import (
        BackgroundJobKind,
        BackgroundJobStatus,
        DeploymentMode,
        VisibilityLevel,
    )
    from openflywheel.contracts.ids import BackgroundJobId, WorkspaceId
    from openflywheel.contracts.workspace import WorkspacePolicy
    from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

    db_path = tmp_path / "book.sqlite"
    migrate_database(db_path, target_version=4)
    database = Database(ConnectionFactory(DatabaseConfig(path=db_path)))
    now = datetime.now(tz=UTC)
    workspace_id = WorkspaceId("ws-dedupe-1")

    with database.write() as conn:
        ws_repo = SqliteWorkspaceRepository()
        workspace = ws_repo.create_workspace(
            conn,
            name="DedupeCo",
            deployment_mode=DeploymentMode.LOCAL,
            policy=WorkspacePolicy(default_visibility=VisibilityLevel.INTERNAL),
            admin_identity_ids=tuple(),
            created_at=now,
            workspace_id=workspace_id,
        )
        conn.execute(
            """
            INSERT INTO agent_sessions
            (id, workspace_id, platform, session_ref, transcript_pointer, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "sess-earliest",
                str(workspace.id),
                "claude_code",
                "dup-ref",
                "transcript://earliest",
                now.isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_sessions
            (id, workspace_id, platform, session_ref, transcript_pointer, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "sess-duplicate",
                str(workspace.id),
                "claude_code",
                "dup-ref",
                "transcript://duplicate",
                (now.replace(microsecond=0)).isoformat(),
            ),
        )

    version = migrate_database(db_path)
    assert version == 5

    job_id = BackgroundJobId("job-preserve-1")
    with database.write() as conn:
        conn.execute(
            """
            INSERT INTO background_jobs
            (id, workspace_id, kind, payload_json, status, lease_owner, lease_expires_at,
             retry_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
            """,
            (
                str(job_id),
                str(workspace_id),
                BackgroundJobKind.TRANSCRIPT_EXTRACT.value,
                '{"episode_id":"e1","session_id":"s1","disable_recursion":true}',
                BackgroundJobStatus.PENDING.value,
                now.isoformat(),
                now.isoformat(),
            ),
        )

    conn = sqlite3.connect(str(db_path))
    sessions = conn.execute(
        """
        SELECT id FROM agent_sessions
        WHERE workspace_id = ? AND platform = ? AND session_ref = ?
        """,
        (str(workspace_id), "claude_code", "dup-ref"),
    ).fetchall()
    index_row = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='index' AND name='idx_agent_sessions_platform_ref'
        """
    ).fetchone()
    job_row = conn.execute(
        "SELECT id, status FROM background_jobs WHERE id = ?",
        (str(job_id),),
    ).fetchone()
    conn.close()

    assert len(sessions) == 1
    assert str(sessions[0][0]) == "sess-earliest"
    assert index_row is not None
    assert job_row is not None
    assert job_row[1] == BackgroundJobStatus.PENDING.value
