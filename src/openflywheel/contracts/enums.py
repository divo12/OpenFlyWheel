"""Shared enumerations."""

from enum import StrEnum


class DeploymentMode(StrEnum):
    LOCAL = "local"
    SHARED = "shared"


class IdentityKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"


class SourceKind(StrEnum):
    GITHUB = "github"
    CLAUDE_CODE = "claude_code"
    CURSOR = "cursor"
    EXPERT_NOTES = "expert_notes"


class ConnectorKind(StrEnum):
    GITHUB = "github"
    CLAUDE_CODE = "claude_code"
    CURSOR = "cursor"
    EXPERT_NOTES = "expert_notes"


class OnboardingStage(StrEnum):
    WORKSPACE = "workspace"
    CONNECT = "connect"
    LOCATE = "locate"
    LOCK = "lock"
    BOOTSTRAP = "bootstrap"
    COMPLETE = "complete"


class OperationStatus(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class TruthSection(StrEnum):
    U1 = "U1"
    U2 = "U2"
    U3 = "U3"
    U4 = "U4"
    U5 = "U5"
    U6 = "U6"
    U7 = "U7"


class ClaimState(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class EdgeKind(StrEnum):
    DERIVED_FROM = "derived_from"
    IN_TENSION_WITH = "in_tension_with"
    SUPERSEDES = "supersedes"


class SystemShape(StrEnum):
    MONOLITH = "monolith"
    MULTI_REPO = "multi_repo"
    SERVICE_MESH = "service_mesh"
    LIBRARY = "library"
    UNKNOWN = "unknown"


class AdmissionDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


class RejectReason(StrEnum):
    JUNK = "junk"
    EXCLUDED_PATH = "excluded_path"
    LIKELY_SECRET = "likely_secret"
    UNSUPPORTED_CONTENT = "unsupported_content"
    DUPLICATE = "duplicate"
    ACL_MISSING = "acl_missing"


class LocatorKind(StrEnum):
    FILE_LINE = "file_line"
    ISSUE_COMMENT = "issue_comment"
    TRANSCRIPT_SPAN = "transcript_span"
    COMMIT = "commit"
    DOCUMENT_SPAN = "document_span"


class AgentEventKind(StrEnum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"


class VisibilityLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    PRIVATE = "private"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    IN_TENSION = "in_tension"


class VerificationDecision(StrEnum):
    PROMOTE = "promote"
    REJECT = "reject"
    LEAVE_IN_TENSION = "leave_in_tension"


class PlatformKind(StrEnum):
    CLAUDE_CODE = "claude_code"
    CURSOR = "cursor"


def parse_platform_kind(value: str) -> PlatformKind:
    normalized = value.strip().replace("-", "_").lower()
    if normalized == PlatformKind.CLAUDE_CODE.value:
        return PlatformKind.CLAUDE_CODE
    if normalized == PlatformKind.CURSOR.value:
        return PlatformKind.CURSOR
    msg = f"Unsupported platform: {value}"
    raise ValueError(msg)


class BackgroundJobKind(StrEnum):
    TRANSCRIPT_EXTRACT = "transcript_extract"
    NOTE_INGEST = "note_ingest"


class BackgroundJobStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"
