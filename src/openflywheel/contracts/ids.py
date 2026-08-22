"""Branded identifier NewTypes."""

from typing import NewType

WorkspaceId = NewType("WorkspaceId", str)
IdentityId = NewType("IdentityId", str)
BoundaryId = NewType("BoundaryId", str)
SourceId = NewType("SourceId", str)
EpisodeId = NewType("EpisodeId", str)
EvidenceAnchorId = NewType("EvidenceAnchorId", str)
ProposalId = NewType("ProposalId", str)
ClaimId = NewType("ClaimId", str)
EdgeId = NewType("EdgeId", str)
CoverageRequirementId = NewType("CoverageRequirementId", str)
CheckpointId = NewType("CheckpointId", str)
AgentSessionId = NewType("AgentSessionId", str)
PinId = NewType("PinId", str)
AuditRejectId = NewType("AuditRejectId", str)
OnboardingRunId = NewType("OnboardingRunId", str)
ManifestVersion = NewType("ManifestVersion", int)
BackgroundJobId = NewType("BackgroundJobId", str)
HarnessRevisionId = NewType("HarnessRevisionId", str)
Sha256Digest = NewType("Sha256Digest", str)
GitCommit = NewType("GitCommit", str)
