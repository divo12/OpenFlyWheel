"""Contract roundtrip and frozen behavior tests."""

from datetime import UTC, datetime

from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.boundary import BoundaryManifest, SourceAuthorityRule
from openflywheel.contracts.enums import SystemShape, VisibilityLevel
from openflywheel.contracts.episode import EpisodeRecord, SourceReference
from openflywheel.contracts.ids import EpisodeId, IdentityId, ManifestVersion, SourceId, WorkspaceId
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.contracts.workspace import WorkspacePolicy


def test_workspace_policy_roundtrip() -> None:
    policy = WorkspacePolicy(default_visibility=VisibilityLevel.INTERNAL, retention_days=90)
    restored = WorkspacePolicy.model_validate_json(policy.model_dump_json())
    assert restored == policy


def test_episode_is_frozen() -> None:
    episode = EpisodeRecord(
        id=EpisodeId("ep-1"),
        workspace_id=WorkspaceId("ws-1"),
        source_ref=SourceReference(
            source_id=SourceId("src-1"),
            external_id="README.md",
            uri="fixture://README.md",
        ),
        content_text="hello",
        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
        event_time=datetime(2026, 8, 15, tzinfo=UTC),
        ingest_time=datetime(2026, 8, 15, tzinfo=UTC),
        checksum="abc",
        content_type="text/plain",
    )
    try:
        episode.content_text = "mutated"  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised


def test_operation_result_failure_fields() -> None:
    result = OperationResult[None].failure(
        code="TEST",
        message="failed",
        root_cause_hint="because test",
        safe_retry=False,
        stop_condition="never",
    )
    assert result.error is not None
    assert result.error.code == "TEST"
    assert result.status.value == "error"


def test_boundary_manifest_roundtrip() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    manifest = BoundaryManifest(
        version=ManifestVersion(1),
        purpose="test",
        system_shape=SystemShape.MULTI_REPO,
        owner_identity_ids=(IdentityId("id-1"),),
        primary_kpi="U3",
        source_authorities=(SourceAuthorityRule(source_slug="github", authority_rank=1),),
        exclusions=("secrets/",),
        locked_at=now,
    )
    restored = BoundaryManifest.model_validate_json(manifest.model_dump_json())
    assert restored.primary_kpi == "U3"
    assert restored.exclusions == ("secrets/",)
