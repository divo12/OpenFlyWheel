"""Dashboard identity and ACL tests."""

import json

from fastapi.testclient import TestClient
from tests.book_helpers import (
    list_proposals,
    owner_identity,
    promote_proposal,
    setup_book_pipeline,
)

from openflywheel.application.workspace_service import WorkspaceService
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.enums import VisibilityLevel
from openflywheel.dashboard.api import create_dashboard_app


def test_dashboard_requires_identity(workspace_home, fixture_root) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    app = create_dashboard_app(database, workspace_id=workspace_id)
    client = TestClient(app)
    response = client.get("/api/overview")
    assert response.status_code == 401


def test_dashboard_rejects_unknown_identity(workspace_home, fixture_root) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    app = create_dashboard_app(database, workspace_id=workspace_id)
    client = TestClient(app)
    response = client.get("/api/overview", headers={"X-OFW-Identity": "forged-identity-id"})
    assert response.status_code == 401


def test_dashboard_docs_disabled(workspace_home, fixture_root) -> None:
    workspace_id, _, home = setup_book_pipeline(workspace_home, fixture_root)
    database = WorkspaceService().load_database(home)
    app = create_dashboard_app(database, workspace_id=workspace_id)
    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_dashboard_acl_two_identities(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id, "Owner Alpha")
    beta_owner = owner_identity(home, workspace_id, "Owner Beta")

    proposal = list_proposals(home, workspace_id)[0]
    promoted = promote_proposal(
        book,
        workspace_id=workspace_id,
        proposal_id=proposal.id,
        verifier_id=owner,
        acl=AclLabel(
            visibility=VisibilityLevel.PRIVATE,
            allowed_identities=(owner,),
        ),
    )
    assert promoted.error is None

    database = WorkspaceService().load_database(home)
    app = create_dashboard_app(database, workspace_id=workspace_id)
    client = TestClient(app)

    owner_resp = client.get("/api/overview", headers={"X-OFW-Identity": str(owner)})
    beta_resp = client.get("/api/overview", headers={"X-OFW-Identity": str(beta_owner)})
    assert owner_resp.status_code == 200
    assert beta_resp.status_code == 200
    owner_payload = owner_resp.json()
    beta_payload = beta_resp.json()
    assert owner_payload["claim_count"] >= beta_payload["claim_count"]


def test_dashboard_pins_filter_private_claim_ids(workspace_home, fixture_root) -> None:
    workspace_id, book, home = setup_book_pipeline(workspace_home, fixture_root)
    owner = owner_identity(home, workspace_id, "Owner Alpha")
    beta_owner = owner_identity(home, workspace_id, "Owner Beta")
    from tests.book_helpers import boundary_id_for_slug, list_proposals, promote_proposal

    proposals = list_proposals(home, workspace_id)
    assert len(proposals) >= 2
    private_promoted = promote_proposal(
        book,
        workspace_id=workspace_id,
        proposal_id=proposals[0].id,
        verifier_id=owner,
        acl=AclLabel(
            visibility=VisibilityLevel.PRIVATE,
            allowed_identities=(owner,),
        ),
    )
    public_promoted = promote_proposal(
        book,
        workspace_id=workspace_id,
        proposal_id=proposals[1].id,
        verifier_id=owner,
        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
    )
    assert private_promoted.error is None and public_promoted.error is None
    assert private_promoted.data is not None and public_promoted.data is not None
    private_claim_id = private_promoted.data.claim_id
    public_claim_id = public_promoted.data.claim_id
    assert private_claim_id is not None and public_claim_id is not None

    boundary_id = boundary_id_for_slug(home, workspace_id, "repo-alpha")
    pinned = book.book_pin(workspace_id=workspace_id, boundary_id=boundary_id)
    assert pinned.error is None

    database = WorkspaceService().load_database(home)
    app = create_dashboard_app(database, workspace_id=workspace_id)
    client = TestClient(app)

    owner_detail = client.get("/api/detail", headers={"X-OFW-Identity": str(owner)}).json()
    beta_detail = client.get("/api/detail", headers={"X-OFW-Identity": str(beta_owner)}).json()

    owner_pins = owner_detail["pins"]
    beta_pins = beta_detail["pins"]
    assert len(owner_pins) >= 1
    owner_claim_ids = {claim_id for pin in owner_pins for claim_id in pin["claim_ids"]}
    assert str(private_claim_id) in owner_claim_ids
    assert str(public_claim_id) in owner_claim_ids

    beta_blob = json.dumps(beta_detail)
    assert str(private_claim_id) not in beta_blob
    if beta_pins:
        beta_claim_ids = {claim_id for pin in beta_pins for claim_id in pin["claim_ids"]}
        assert str(private_claim_id) not in beta_claim_ids
        assert str(public_claim_id) in beta_claim_ids or len(beta_claim_ids) == 0
    assert beta_detail["overview"]["pin_count"] == len(beta_pins)
