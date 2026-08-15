"""Construct typed row dataclasses from sqlite3.Row without Any leakage."""

from __future__ import annotations

import sqlite3

from openflywheel.store.rows import (
    AuditRejectRow,
    BoundaryRow,
    CheckpointRow,
    ClaimRow,
    CoverageRequirementRow,
    EdgeRow,
    EpisodeRow,
    EvidenceAnchorRow,
    IdentityRow,
    OnboardingRow,
    PinRow,
    ProposalRow,
    SourceRow,
    WorkspaceRow,
)
from openflywheel.store.sqlite_access import cell_int, cell_optional_str, cell_str


def workspace_row(raw: sqlite3.Row) -> WorkspaceRow:
    return WorkspaceRow(
        id=cell_str(raw, "id"),
        name=cell_str(raw, "name"),
        deployment_mode=cell_str(raw, "deployment_mode"),
        policy_json=cell_str(raw, "policy_json"),
        admin_identity_ids_json=cell_str(raw, "admin_identity_ids_json"),
        created_at=cell_str(raw, "created_at"),
    )


def identity_row(raw: sqlite3.Row) -> IdentityRow:
    return IdentityRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        kind=cell_str(raw, "kind"),
        display_name=cell_str(raw, "display_name"),
        acl_json=cell_str(raw, "acl_json"),
        created_at=cell_str(raw, "created_at"),
    )


def boundary_row(raw: sqlite3.Row) -> BoundaryRow:
    return BoundaryRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        name=cell_str(raw, "name"),
        slug=cell_str(raw, "slug"),
        component_paths_json=cell_str(raw, "component_paths_json"),
        manifest_json=cell_optional_str(raw, "manifest_json"),
        created_at=cell_str(raw, "created_at"),
    )


def source_row(raw: sqlite3.Row) -> SourceRow:
    return SourceRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        kind=cell_str(raw, "kind"),
        slug=cell_str(raw, "slug"),
        display_name=cell_str(raw, "display_name"),
        capability_json=cell_str(raw, "capability_json"),
        root_path=cell_optional_str(raw, "root_path"),
        created_at=cell_str(raw, "created_at"),
    )


def episode_row(raw: sqlite3.Row) -> EpisodeRow:
    return EpisodeRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        source_id=cell_str(raw, "source_id"),
        external_id=cell_str(raw, "external_id"),
        uri=cell_str(raw, "uri"),
        content_text=cell_str(raw, "content_text"),
        acl_json=cell_str(raw, "acl_json"),
        event_time=cell_str(raw, "event_time"),
        ingest_time=cell_str(raw, "ingest_time"),
        checksum=cell_str(raw, "checksum"),
        content_type=cell_str(raw, "content_type"),
    )


def evidence_anchor_row(raw: sqlite3.Row) -> EvidenceAnchorRow:
    return EvidenceAnchorRow(
        id=cell_str(raw, "id"),
        episode_id=cell_str(raw, "episode_id"),
        locator_kind=cell_str(raw, "locator_kind"),
        locator_value=cell_str(raw, "locator_value"),
        label=cell_str(raw, "label"),
    )


def checkpoint_row(raw: sqlite3.Row) -> CheckpointRow:
    return CheckpointRow(
        id=cell_str(raw, "id"),
        source_id=cell_str(raw, "source_id"),
        cursor_value=cell_str(raw, "cursor_value"),
        updated_at=cell_str(raw, "updated_at"),
    )


def audit_reject_row(raw: sqlite3.Row) -> AuditRejectRow:
    return AuditRejectRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        source_id=cell_str(raw, "source_id"),
        external_id=cell_str(raw, "external_id"),
        reason=cell_str(raw, "reason"),
        detail=cell_str(raw, "detail"),
        rejected_at=cell_str(raw, "rejected_at"),
    )


def onboarding_row(raw: sqlite3.Row) -> OnboardingRow:
    return OnboardingRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        stage=cell_str(raw, "stage"),
        connect_json=cell_optional_str(raw, "connect_json"),
        locate_json=cell_optional_str(raw, "locate_json"),
        lock_json=cell_optional_str(raw, "lock_json"),
        updated_at=cell_str(raw, "updated_at"),
    )


def proposal_row(raw: sqlite3.Row) -> ProposalRow:
    return ProposalRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        boundary_id=cell_str(raw, "boundary_id"),
        what=cell_str(raw, "what"),
        how=cell_str(raw, "how"),
        section=cell_str(raw, "section"),
        proposer_identity_id=cell_str(raw, "proposer_identity_id"),
        anchor_ids_json=cell_str(raw, "anchor_ids_json"),
        status=cell_str(raw, "status"),
        idempotency_key=cell_str(raw, "idempotency_key"),
        created_at=cell_str(raw, "created_at"),
    )


def claim_row(raw: sqlite3.Row) -> ClaimRow:
    return ClaimRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        boundary_id=cell_str(raw, "boundary_id"),
        what=cell_str(raw, "what"),
        how=cell_str(raw, "how"),
        section=cell_str(raw, "section"),
        state=cell_str(raw, "state"),
        authority_identity_id=cell_str(raw, "authority_identity_id"),
        acl_json=cell_str(raw, "acl_json"),
        valid_from=cell_str(raw, "valid_from"),
        valid_to=cell_optional_str(raw, "valid_to"),
        source_proposal_id=cell_optional_str(raw, "source_proposal_id"),
    )


def edge_row(raw: sqlite3.Row) -> EdgeRow:
    return EdgeRow(
        id=cell_str(raw, "id"),
        kind=cell_str(raw, "kind"),
        from_claim_id=cell_str(raw, "from_claim_id"),
        to_claim_id=cell_str(raw, "to_claim_id"),
        note=cell_str(raw, "note"),
    )


def coverage_requirement_row(raw: sqlite3.Row) -> CoverageRequirementRow:
    return CoverageRequirementRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        boundary_id=cell_str(raw, "boundary_id"),
        section=cell_str(raw, "section"),
        slot_key=cell_str(raw, "slot_key"),
        description=cell_str(raw, "description"),
        required_for_shape=cell_str(raw, "required_for_shape"),
        verified=cell_int(raw, "verified"),
    )


def pin_row(raw: sqlite3.Row) -> PinRow:
    return PinRow(
        id=cell_str(raw, "id"),
        workspace_id=cell_str(raw, "workspace_id"),
        boundary_id=cell_str(raw, "boundary_id"),
        manifest_version=cell_int(raw, "manifest_version"),
        claim_ids_json=cell_str(raw, "claim_ids_json"),
        created_at=cell_str(raw, "created_at"),
    )
