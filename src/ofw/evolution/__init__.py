"""Prepared-experiment evolution contracts."""

from ofw.evolution.candidate import (
    CandidateBlocker,
    CandidateBlockerCode,
    CandidateErrorCode,
    CandidateExecutionInput,
    CandidateExecutionObservation,
    CandidateFailure,
    CandidateId,
    CandidateOutcomeReceipt,
    CandidatePhase,
    CandidateStatus,
)
from ofw.evolution.candidate_git import CandidateGitGateway
from ofw.evolution.candidate_langfuse import LangfuseCandidateTraceLocator
from ofw.evolution.candidate_service import CandidateExecutionService
from ofw.evolution.gate import (
    PromotionDecision,
    PromotionReason,
    PromotionStatus,
    decide_promotion,
)
from ofw.evolution.hypothesis import (
    FailurePatternReference,
    FailurePatternReferenceInput,
    HarnessChangeTarget,
    HarnessChangeTargetInput,
    HarnessHypothesis,
    HypothesisErrorCode,
    HypothesisFailure,
    HypothesisId,
    HypothesisObservation,
    HypothesisService,
    HypothesisStatus,
    RecordHypothesisInput,
)
from ofw.evolution.hypothesis_repository import FileHypothesisRepository

__all__ = [
    "CandidateBlocker",
    "CandidateBlockerCode",
    "CandidateErrorCode",
    "CandidateExecutionInput",
    "CandidateExecutionObservation",
    "CandidateExecutionService",
    "CandidateFailure",
    "CandidateGitGateway",
    "CandidateId",
    "CandidateOutcomeReceipt",
    "CandidatePhase",
    "CandidateStatus",
    "PromotionDecision",
    "PromotionReason",
    "PromotionStatus",
    "FailurePatternReference",
    "FailurePatternReferenceInput",
    "FileHypothesisRepository",
    "HarnessChangeTarget",
    "HarnessChangeTargetInput",
    "HarnessHypothesis",
    "HypothesisErrorCode",
    "HypothesisFailure",
    "HypothesisId",
    "HypothesisObservation",
    "HypothesisService",
    "HypothesisStatus",
    "LangfuseCandidateTraceLocator",
    "RecordHypothesisInput",
    "decide_promotion",
]
