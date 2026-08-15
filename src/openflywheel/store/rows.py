"""Typed SQLite row dataclasses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceRow:
    id: str
    name: str
    deployment_mode: str
    policy_json: str
    admin_identity_ids_json: str
    created_at: str


@dataclass(frozen=True)
class IdentityRow:
    id: str
    workspace_id: str
    kind: str
    display_name: str
    acl_json: str
    created_at: str


@dataclass(frozen=True)
class BoundaryRow:
    id: str
    workspace_id: str
    name: str
    slug: str
    component_paths_json: str
    manifest_json: str | None
    created_at: str


@dataclass(frozen=True)
class SourceRow:
    id: str
    workspace_id: str
    kind: str
    slug: str
    display_name: str
    capability_json: str
    root_path: str | None
    created_at: str


@dataclass(frozen=True)
class EpisodeRow:
    id: str
    workspace_id: str
    source_id: str
    external_id: str
    uri: str
    content_text: str
    acl_json: str
    event_time: str
    ingest_time: str
    checksum: str
    content_type: str


@dataclass(frozen=True)
class EvidenceAnchorRow:
    id: str
    episode_id: str
    locator_kind: str
    locator_value: str
    label: str


@dataclass(frozen=True)
class CheckpointRow:
    id: str
    source_id: str
    cursor_value: str
    updated_at: str


@dataclass(frozen=True)
class AuditRejectRow:
    id: str
    workspace_id: str
    source_id: str
    external_id: str
    reason: str
    detail: str
    rejected_at: str


@dataclass(frozen=True)
class OnboardingRow:
    id: str
    workspace_id: str
    stage: str
    connect_json: str | None
    locate_json: str | None
    lock_json: str | None
    updated_at: str


@dataclass(frozen=True)
class ProposalRow:
    id: str
    workspace_id: str
    boundary_id: str
    what: str
    how: str
    section: str
    proposer_identity_id: str
    anchor_ids_json: str
    status: str
    idempotency_key: str
    created_at: str


@dataclass(frozen=True)
class ClaimRow:
    id: str
    workspace_id: str
    boundary_id: str
    what: str
    how: str
    section: str
    state: str
    authority_identity_id: str
    acl_json: str
    valid_from: str
    valid_to: str | None
    source_proposal_id: str | None


@dataclass(frozen=True)
class EdgeRow:
    id: str
    kind: str
    from_claim_id: str
    to_claim_id: str
    note: str


@dataclass(frozen=True)
class CoverageRequirementRow:
    id: str
    workspace_id: str
    boundary_id: str
    section: str
    slot_key: str
    description: str
    required_for_shape: str
    verified: int


@dataclass(frozen=True)
class PinRow:
    id: str
    workspace_id: str
    boundary_id: str
    manifest_version: int
    claim_ids_json: str
    created_at: str
