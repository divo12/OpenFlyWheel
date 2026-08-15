"""Regression tests for Grok D-F hardening findings."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.book_helpers import (
    boundary_id_for_slug,
    list_proposals,
    owner_identity,
    promote_proposal,
    setup_book_pipeline,
)
from typer.testing import CliRunner

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.cli.main import app
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.book import BookContextRequest, ProposeManualRequest, VerifyRequest
from openflywheel.contracts.enums import (
    ClaimState,
    IdentityKind,
    PlatformKind,
    TruthSection,
    VerificationDecision,
    VisibilityLevel,
)
from openflywheel.contracts.ids import EvidenceAnchorId
from openflywheel.retrieval.fts_query import escape_fts_query

runner = CliRunner()
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _u3_verified_count(home, workspace_id) -> int:
    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM coverage_requirements
            WHERE workspace_id = ? AND section = 'U3' AND verified = 1
            """,
            (str(workspace_id),),
        ).fetchone()
    return int(row["cnt"]) if row is not None else 0


def test_coverage_seed_never_overwrites_verified(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    u3_props = [p for p in list_proposals(home, workspace_id) if p.section == TruthSection.U3]
    for proposal in u3_props[:2]:
        promote_proposal(
            book, workspace_id=workspace_id, proposal_id=proposal.id, verifier_id=owner
        )
    before = _u3_verified_count(home, workspace_id)
    assert before > 0
    for _ in range(3):
        result = book.coverage_gaps(workspace_id=workspace_id)
        assert result.error is None
    after = _u3_verified_count(home, workspace_id)
    assert after == before


def test_unauthorized_verify_performs_zero_writes(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    proposals = list_proposals(home, workspace_id)
    database = WorkspaceService().load_database(home)
    with database.write() as conn:
        from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

        stranger = SqliteWorkspaceRepository().create_identity(
            conn,
            workspace_id=workspace_id,
            kind=IdentityKind.HUMAN,
            display_name="stranger",
            created_at=datetime.now(tz=UTC),
        )
    before = book._verify.count_before_verify(workspace_id)
    result = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=proposals[0].id,
            decision=VerificationDecision.PROMOTE,
            verifier_identity_id=stranger.id,
        ),
    )
    after = book._verify.count_before_verify(workspace_id)
    assert result.error is not None
    assert result.error.code == "VERIFY_UNAUTHORIZED"
    assert before == after


def test_agent_owner_cannot_verify(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    proposals = list_proposals(home, workspace_id)
    database = WorkspaceService().load_database(home)
    with database.write() as conn:
        from openflywheel.contracts.boundary import BoundaryManifest
        from openflywheel.store.repos.boundary_repo import SqliteBoundaryRepository
        from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

        agent = SqliteWorkspaceRepository().create_identity(
            conn,
            workspace_id=workspace_id,
            kind=IdentityKind.AGENT,
            display_name="agent-verifier",
            created_at=datetime.now(tz=UTC),
        )
        boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
        boundaries = SqliteBoundaryRepository()
        boundary = boundaries.get_by_id(conn, boundary_id)
        assert boundary is not None and boundary.manifest is not None
        manifest = BoundaryManifest(
            version=boundary.manifest.version,
            purpose=boundary.manifest.purpose,
            system_shape=boundary.manifest.system_shape,
            owner_identity_ids=boundary.manifest.owner_identity_ids + (agent.id,),
            primary_kpi=boundary.manifest.primary_kpi,
            source_authorities=boundary.manifest.source_authorities,
            exclusions=boundary.manifest.exclusions,
            locked_at=boundary.manifest.locked_at,
        )
        boundaries.upsert_boundary(
            conn,
            workspace_id=workspace_id,
            name=boundary.name,
            slug=boundary.slug,
            component_paths=boundary.component_paths,
            manifest=manifest,
            created_at=boundary.created_at,
            boundary_id=boundary.id,
        )
    before = book._verify.count_before_verify(workspace_id)
    result = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=proposals[0].id,
            decision=VerificationDecision.PROMOTE,
            verifier_identity_id=agent.id,
        ),
    )
    after = book._verify.count_before_verify(workspace_id)
    assert result.error is not None
    assert result.error.code == "VERIFY_UNAUTHORIZED"
    assert before == after


def test_pin_supersede_read_shows_original_active_state(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    u3_props = [
        p
        for p in list_proposals(home, workspace_id)
        if p.section == TruthSection.U3 and p.boundary_id == boundary_id
    ]
    assert len(u3_props) >= 2
    first = promote_proposal(
        book, workspace_id=workspace_id, proposal_id=u3_props[0].id, verifier_id=owner
    )
    assert first.data is not None
    old_id = first.data.claim_id
    assert old_id is not None
    original_what = u3_props[0].what
    original_how = u3_props[0].how

    pin_result = book.book_pin(workspace_id=workspace_id, boundary_id=boundary_id)
    assert pin_result.error is None
    assert pin_result.data is not None
    pin_id = pin_result.data.pin_id

    promote_proposal(
        book,
        workspace_id=workspace_id,
        proposal_id=u3_props[1].id,
        verifier_id=owner,
        supersedes_claim_id=old_id,
    )

    detail = book.book_get(
        workspace_id=workspace_id,
        identity_id=owner,
        claim_id=old_id,
        pin_id=pin_id,
    )
    assert detail.error is None
    assert detail.data is not None
    claim = detail.data.claim
    assert claim.state == ClaimState.ACTIVE
    assert claim.valid_to is None
    assert claim.what == original_what
    assert claim.how == original_how


def test_pin_packet_does_not_expand_post_pin_claims(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    proposals = [p for p in list_proposals(home, workspace_id) if p.boundary_id == boundary_id]
    for proposal in proposals[:2]:
        promote_proposal(
            book, workspace_id=workspace_id, proposal_id=proposal.id, verifier_id=owner
        )
    pin_result = book.book_pin(workspace_id=workspace_id, boundary_id=boundary_id)
    assert pin_result.error is None
    assert pin_result.data is not None
    pin_count = pin_result.data.claim_count
    pin_id = pin_result.data.pin_id

    for proposal in proposals[2:4]:
        promote_proposal(
            book, workspace_id=workspace_id, proposal_id=proposal.id, verifier_id=owner
        )

    ctx = book.book_context(
        BookContextRequest(
            workspace_id=workspace_id,
            identity_id=owner,
            query="package",
            boundary_id=boundary_id,
            pin_id=pin_id,
        )
    )
    assert ctx.error is None
    assert ctx.data is not None
    assert len(ctx.data.packet.claims) == pin_count


def test_acl_omits_edges_and_markdown_without_private_leak(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner_alpha = owner_identity(home, workspace_id, "Owner Alpha")
    owner_beta = owner_identity(home, workspace_id, "Owner Beta")
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    database = WorkspaceService().load_database(home)

    with database.write() as conn:
        from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

        private_reader = SqliteWorkspaceRepository().create_identity(
            conn,
            workspace_id=workspace_id,
            kind=IdentityKind.HUMAN,
            display_name="private-reader",
            created_at=datetime.now(tz=UTC),
        )

    proposals = [p for p in list_proposals(home, workspace_id) if p.boundary_id == boundary_id]
    public = promote_proposal(
        book, workspace_id=workspace_id, proposal_id=proposals[0].id, verifier_id=owner_alpha
    )
    assert public.data is not None
    public_id = public.data.claim_id

    private_acl = AclLabel(
        visibility=VisibilityLevel.PRIVATE,
        allowed_identities=(private_reader.id,),
    )
    private = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=proposals[1].id,
            decision=VerificationDecision.PROMOTE,
            verifier_identity_id=owner_alpha,
            acl=private_acl,
            tension_with_claim_id=public_id,
        ),
    )
    assert private.error is None
    assert private.data is not None
    private_id = private.data.claim_id
    assert private_id is not None
    private_what = proposals[1].what

    beta_ctx = book.book_context(
        BookContextRequest(
            workspace_id=workspace_id,
            identity_id=owner_beta,
            query=proposals[0].what.split()[0],
            boundary_id=boundary_id,
        )
    )
    assert beta_ctx.error is None
    assert beta_ctx.data is not None
    markdown = beta_ctx.data.markdown
    assert str(private_id) not in markdown
    assert private_what not in markdown
    assert not UUID_PATTERN.search(markdown.replace(str(public_id), ""))
    for edge in beta_ctx.data.packet.tensions:
        assert edge.from_claim_id in {c.id for c in beta_ctx.data.packet.claims}
        assert edge.to_claim_id in {c.id for c in beta_ctx.data.packet.claims}


def test_book_get_history_acl_filters_private_same_what(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id, "Owner Alpha")
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    database = WorkspaceService().load_database(home)

    with database.write() as conn:
        from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

        private_reader = SqliteWorkspaceRepository().create_identity(
            conn,
            workspace_id=workspace_id,
            kind=IdentityKind.HUMAN,
            display_name="hist-reader",
            created_at=datetime.now(tz=UTC),
        )

    proposals = [p for p in list_proposals(home, workspace_id) if p.boundary_id == boundary_id]
    first = promote_proposal(
        book, workspace_id=workspace_id, proposal_id=proposals[0].id, verifier_id=owner
    )
    assert first.data is not None
    old_id = first.data.claim_id
    assert old_id is not None

    private_acl = AclLabel(
        visibility=VisibilityLevel.PRIVATE,
        allowed_identities=(private_reader.id,),
    )
    second = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=proposals[1].id,
            decision=VerificationDecision.PROMOTE,
            verifier_identity_id=owner,
            acl=private_acl,
            supersedes_claim_id=old_id,
        ),
    )
    assert second.data is not None
    new_id = second.data.claim_id
    assert new_id is not None

    owner_beta = owner_identity(home, workspace_id, "Owner Beta")
    public_denied = book.book_get(
        workspace_id=workspace_id,
        identity_id=owner_beta,
        claim_id=new_id,
    )
    assert public_denied.error is not None
    assert public_denied.error.code == "GET_ACL_DENIED"

    old_detail = book.book_get(
        workspace_id=workspace_id,
        identity_id=owner_beta,
        claim_id=old_id,
    )
    assert old_detail.error is None
    assert old_detail.data is not None
    assert new_id not in {c.id for c in old_detail.data.history}

    private_detail = book.book_get(
        workspace_id=workspace_id,
        identity_id=private_reader.id,
        claim_id=new_id,
    )
    assert private_detail.error is None
    assert private_detail.data is not None
    private_history_ids = {c.id for c in private_detail.data.history}
    assert old_id in private_history_ids


def test_fts_acl_before_limit_many_private_one_public(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id, "Owner Alpha")
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    database = WorkspaceService().load_database(home)

    with database.write() as conn:
        from openflywheel.store.repos.workspace_repo import SqliteWorkspaceRepository

        reader = SqliteWorkspaceRepository().create_identity(
            conn,
            workspace_id=workspace_id,
            kind=IdentityKind.HUMAN,
            display_name="fts-reader",
            created_at=datetime.now(tz=UTC),
        )
    reader_id = reader.id
    proposals = [p for p in list_proposals(home, workspace_id) if p.boundary_id == boundary_id]
    private_acl = AclLabel(
        visibility=VisibilityLevel.PRIVATE,
        allowed_identities=(reader_id,),
    )
    public_term = "alphacore"
    for proposal in proposals[1:]:
        book.book_verify(
            workspace_id=workspace_id,
            request=VerifyRequest(
                proposal_id=proposal.id,
                decision=VerificationDecision.PROMOTE,
                verifier_identity_id=owner,
                acl=private_acl,
            ),
        )
    public = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=proposals[0].id,
            decision=VerificationDecision.PROMOTE,
            verifier_identity_id=owner,
        ),
    )
    assert public.error is None

    ctx = book.book_context(
        BookContextRequest(
            workspace_id=workspace_id,
            identity_id=reader_id,
            query=public_term,
            boundary_id=boundary_id,
        )
    )
    assert ctx.error is None
    assert ctx.data is not None
    assert len(ctx.data.packet.claims) >= 1
    assert any(public_term.lower() in c.what.lower() for c in ctx.data.packet.claims)


def test_fts_trigger_indexes_active_claims_only(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    proposals = list_proposals(home, workspace_id)[:1]
    promoted = promote_proposal(
        book, workspace_id=workspace_id, proposal_id=proposals[0].id, verifier_id=owner
    )
    assert promoted.data is not None
    claim_id = promoted.data.claim_id
    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        row = conn.execute(
            "SELECT claim_id FROM claim_fts WHERE claim_id = ?",
            (str(claim_id),),
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE claims SET state = 'superseded', valid_to = ? WHERE id = ?",
            (datetime.now(tz=UTC).isoformat(), str(claim_id)),
        )
        gone = conn.execute(
            "SELECT claim_id FROM claim_fts WHERE claim_id = ?",
            (str(claim_id),),
        ).fetchone()
    assert gone is None


def test_pin_immutability_triggers(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    promote_proposal(
        book,
        workspace_id=workspace_id,
        proposal_id=list_proposals(home, workspace_id)[0].id,
        verifier_id=owner,
    )
    pin_result = book.book_pin(workspace_id=workspace_id, boundary_id=boundary_id)
    assert pin_result.data is not None
    pin_id = pin_result.data.pin_id
    database = WorkspaceService().load_database(home)
    with database.write() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE pins SET boundary_id = boundary_id WHERE id = ?",
                (str(pin_id),),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM pins WHERE id = ?", (str(pin_id),))


def test_claim_propose_validates_anchors_and_idempotent(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    proposal = list_proposals(home, workspace_id)[0]
    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        anchor_row = conn.execute(
            """
            SELECT ea.id FROM evidence_anchors ea
            JOIN proposals p ON p.anchor_ids_json LIKE '%' || ea.id || '%'
            WHERE p.id = ?
            LIMIT 1
            """,
            (str(proposal.id),),
        ).fetchone()
    assert anchor_row is not None
    anchor_id = EvidenceAnchorId(str(anchor_row["id"]))

    bad = book.claim_propose(
        ProposeManualRequest(
            workspace_id=workspace_id,
            boundary_id=boundary_id,
            what="Manual what",
            how="Manual how",
            section=TruthSection.U5,
            proposer_identity_id=owner,
            anchor_ids=(EvidenceAnchorId("00000000-0000-0000-0000-000000000099"),),
        )
    )
    assert bad.error is not None
    assert bad.error.code == "PROPOSE_ANCHOR_NOT_FOUND"

    req = ProposeManualRequest(
        workspace_id=workspace_id,
        boundary_id=boundary_id,
        what="Manual what",
        how="Manual how",
        section=TruthSection.U5,
        proposer_identity_id=owner,
        anchor_ids=(anchor_id,),
    )
    first = book.claim_propose(req)
    second = book.claim_propose(req)
    assert first.error is None and second.error is None
    assert first.data == second.data


def test_sqlite_error_message_is_generic() -> None:
    from openflywheel.store.exceptions import map_sqlite_error

    mapped = map_sqlite_error(sqlite3.OperationalError("secret internal detail"))
    assert "secret internal detail" not in mapped.message
    assert mapped.code == "SQLITE_ERROR"


def test_escape_fts_query_quotes_operators() -> None:
    assert escape_fts_query("foo OR bar") == '"foo" "OR" "bar"'
    assert escape_fts_query('"quoted"') == '"""quoted"""'


def test_episode_record_requires_envelope(workspace_home, fixture_root) -> None:
    from pathlib import Path

    from tests.agent_helpers import episode_request, setup_agent_pipeline

    workspace_id, book, home = setup_agent_pipeline(workspace_home, fixture_root)
    transcript_root = Path(__file__).resolve().parents[2] / "fixtures" / "agent-transcripts"
    request = episode_request(
        home=home,
        workspace_id=workspace_id,
        platform=PlatformKind.CLAUDE_CODE,
        session_ref="deferred-check",
        transcript_path=transcript_root / "claude-session.jsonl",
        fixture_root=fixture_root,
    )
    result = book.episode_record(request)
    assert result.error is None
    assert result.data is not None
    assert result.data.claims_created == 0


def test_cli_propose_and_deferred_exit_codes(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    _ = workspace_id
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    proposal = list_proposals(home, workspace_id)[0]
    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        anchor_row = conn.execute(
            """
            SELECT ea.id FROM evidence_anchors ea
            JOIN proposals p ON p.anchor_ids_json LIKE '%' || ea.id || '%'
            WHERE p.id = ?
            LIMIT 1
            """,
            (str(proposal.id),),
        ).fetchone()
    assert anchor_row is not None
    anchor_id = str(anchor_row["id"])

    propose = runner.invoke(
        app,
        [
            "book",
            "propose",
            "CLI what",
            "CLI how",
            "--home",
            str(home),
            "--boundary",
            str(boundary_id),
            "--identity",
            str(owner),
            "--anchor",
            anchor_id,
        ],
    )
    assert propose.exit_code == 0
    payload = json.loads(propose.stdout)
    assert payload["status"] == "success"

    transcript = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "agent-transcripts"
        / "claude-session.jsonl"
    )
    fixtures_root = transcript.parent.parent
    episode = runner.invoke(
        app,
        [
            "book",
            "episode-record",
            "--home",
            str(home),
            "--platform",
            "claude_code",
            "--session-ref",
            "cli-session",
            "--transcript",
            str(transcript),
            "--agent-home",
            str(fixtures_root),
            "--project-root",
            str(fixture_root),
            "--identity",
            str(owner),
        ],
    )
    assert episode.exit_code == 0
    episode_payload = json.loads(episode.stdout)
    assert episode_payload["status"] == "success"
    assert episode_payload["data"]["claims_created"] == 0


def test_pin_preserves_shared_anchor_per_claim(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    seed_proposal = list_proposals(home, workspace_id)[0]
    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        anchor_row = conn.execute(
            """
            SELECT ea.id FROM evidence_anchors ea
            JOIN proposals p ON p.anchor_ids_json LIKE '%' || ea.id || '%'
            WHERE p.id = ?
            LIMIT 1
            """,
            (str(seed_proposal.id),),
        ).fetchone()
    assert anchor_row is not None
    shared_anchor = EvidenceAnchorId(str(anchor_row["id"]))

    first = book.claim_propose(
        ProposeManualRequest(
            workspace_id=workspace_id,
            boundary_id=boundary_id,
            what="Shared anchor claim A",
            how="First how path",
            section=TruthSection.U5,
            proposer_identity_id=owner,
            anchor_ids=(shared_anchor,),
        )
    )
    second = book.claim_propose(
        ProposeManualRequest(
            workspace_id=workspace_id,
            boundary_id=boundary_id,
            what="Shared anchor claim B",
            how="Second how path",
            section=TruthSection.U6,
            proposer_identity_id=owner,
            anchor_ids=(shared_anchor,),
        )
    )
    assert first.error is None and second.error is None
    assert first.data != second.data
    for proposal_id in (first.data, second.data):
        assert proposal_id is not None
        promote_proposal(
            book, workspace_id=workspace_id, proposal_id=proposal_id, verifier_id=owner
        )

    pin_result = book.book_pin(workspace_id=workspace_id, boundary_id=boundary_id)
    assert pin_result.error is None
    assert pin_result.data is not None

    with database.read() as conn:
        rows = conn.execute(
            """
            SELECT claim_id FROM pin_anchor_snapshots
            WHERE pin_id = ? AND anchor_id = ?
            """,
            (str(pin_result.data.pin_id), str(shared_anchor)),
        ).fetchall()
    assert len(rows) == 2
    assert len({str(row["claim_id"]) for row in rows}) == 2


@pytest.mark.parametrize(
    "table",
    ("pin_claim_snapshots", "pin_anchor_snapshots", "pin_edge_snapshots"),
)
def test_pin_snapshot_tables_immutable(workspace_home, fixture_root, table: str) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    proposals = [p for p in list_proposals(home, workspace_id) if p.boundary_id == boundary_id]
    first = promote_proposal(
        book, workspace_id=workspace_id, proposal_id=proposals[0].id, verifier_id=owner
    )
    assert first.data is not None
    counterpart = first.data.claim_id
    assert counterpart is not None
    promote_proposal(
        book,
        workspace_id=workspace_id,
        proposal_id=proposals[1].id,
        verifier_id=owner,
        tension_with_claim_id=counterpart,
    )
    pin_result = book.book_pin(workspace_id=workspace_id, boundary_id=boundary_id)
    assert pin_result.data is not None
    database = WorkspaceService().load_database(home)
    with database.write() as conn:
        row = conn.execute(f"SELECT rowid FROM {table} LIMIT 1").fetchone()
        assert row is not None, f"expected rows in {table}"
        rowid = row["rowid"]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"UPDATE {table} SET pin_id = pin_id WHERE rowid = ?", (rowid,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))


def test_context_reports_gaps_before_coverage_write(workspace_home, fixture_root) -> None:
    from tests.helpers import onboard_and_lock

    from openflywheel.application.book_app import BookApplication

    onboard_and_lock(workspace_home, fixture_root)
    ws = WorkspaceService()
    database = ws.load_database(workspace_home)
    config = ws.read_config(workspace_home)
    boundary_id = boundary_id_for_slug(workspace_home, config.workspace_id, "repo-alpha")
    owner = owner_identity(workspace_home, config.workspace_id, "Owner Alpha")

    with database.read() as conn:
        seeded = conn.execute(
            "SELECT COUNT(*) AS cnt FROM coverage_requirements WHERE boundary_id = ?",
            (str(boundary_id),),
        ).fetchone()
        assert seeded is not None
        assert int(seeded["cnt"]) > 0

    book = BookApplication(database)
    ctx = book.book_context(
        BookContextRequest(
            workspace_id=config.workspace_id,
            identity_id=owner,
            query="architecture",
            boundary_id=boundary_id,
        )
    )
    assert ctx.error is None
    assert ctx.data is not None
    assert len(ctx.data.packet.gaps) > 0


def test_manual_propose_distinct_when_section_or_how_differs(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    seed_proposal = list_proposals(home, workspace_id)[0]
    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        anchor_row = conn.execute(
            """
            SELECT ea.id FROM evidence_anchors ea
            JOIN proposals p ON p.anchor_ids_json LIKE '%' || ea.id || '%'
            WHERE p.id = ?
            LIMIT 1
            """,
            (str(seed_proposal.id),),
        ).fetchone()
    assert anchor_row is not None
    anchor_id = EvidenceAnchorId(str(anchor_row["id"]))
    shared = dict(
        workspace_id=workspace_id,
        boundary_id=boundary_id,
        what="Same what text",
        proposer_identity_id=owner,
        anchor_ids=(anchor_id,),
    )
    first = book.claim_propose(ProposeManualRequest(**shared, how="How A", section=TruthSection.U5))
    second = book.claim_propose(
        ProposeManualRequest(**shared, how="How B", section=TruthSection.U5)
    )
    third = book.claim_propose(ProposeManualRequest(**shared, how="How A", section=TruthSection.U6))
    assert first.error is None and second.error is None and third.error is None
    ids = {first.data, second.data, third.data}
    assert len(ids) == 3


def test_leave_in_tension_without_counterpart_zero_writes(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    pending = next(p for p in list_proposals(home, workspace_id) if p.status.value == "pending")
    before = book._verify.count_before_verify(workspace_id)
    result = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=pending.id,
            decision=VerificationDecision.LEAVE_IN_TENSION,
            verifier_identity_id=owner,
        ),
    )
    after = book._verify.count_before_verify(workspace_id)
    assert result.error is not None
    assert result.error.code == "VERIFY_TENSION_REQUIRED"
    assert before == after


def test_leave_in_tension_never_marks_coverage_verified(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id)
    u3_props = [p for p in list_proposals(home, workspace_id) if p.section == TruthSection.U3]
    assert len(u3_props) >= 2
    first = promote_proposal(
        book, workspace_id=workspace_id, proposal_id=u3_props[0].id, verifier_id=owner
    )
    assert first.data is not None
    counterpart = first.data.claim_id
    assert counterpart is not None
    verified_before = _u3_verified_count(home, workspace_id)

    tension = book.book_verify(
        workspace_id=workspace_id,
        request=VerifyRequest(
            proposal_id=u3_props[1].id,
            decision=VerificationDecision.LEAVE_IN_TENSION,
            verifier_identity_id=owner,
            tension_with_claim_id=counterpart,
        ),
    )
    assert tension.error is None
    assert tension.data is not None
    assert tension.data.claim_id is not None

    database = WorkspaceService().load_database(home)
    with database.read() as conn:
        claim = conn.execute(
            "SELECT state FROM claims WHERE id = ?",
            (str(tension.data.claim_id),),
        ).fetchone()
    assert claim is not None
    assert claim["state"] == "proposed"
    assert _u3_verified_count(home, workspace_id) == verified_before
