"""Installed-package typing contract."""

from importlib.util import find_spec
from pathlib import Path

import ofw as package


def test_package_declares_inline_types() -> None:
    package_file = package.__file__
    assert package_file is not None
    assert Path(package_file).with_name("py.typed").is_file()


def test_namespace_excludes_removed_collection_api() -> None:
    assert "collect" not in package.__all__
    assert "CollectionResult" not in package.__all__


def test_package_excludes_removed_harness_plane() -> None:
    removed = {
        "CanaryCase",
        "CommandLoop",
        "E2BSandbox",
        "Harness",
        "HarnessRevision",
        "ProcessLimits",
        "RunResult",
        "ofw",
    }

    assert removed.isdisjoint(package.__all__)
    assert find_spec("ofw.harness") is None
    assert find_spec("ofw.runtime") is None


def test_namespace_exports_authoritative_outcome_contract() -> None:
    assert "EvaluatedRunBlocker" in package.__all__
    assert "EvaluatedRunReceipt" in package.__all__
    assert "EvaluatedTaskReceipt" in package.__all__
    assert "OutcomeEvaluation" in package.__all__
    assert "RunSide" in package.__all__
    assert "LangfuseOutcomeStore" in package.__all__
    assert "EvidenceReference" in package.__all__
    assert "TaskId" in package.__all__
    assert "TraceId" in package.__all__
    assert "VerifierId" in package.__all__
    assert "VerifierResult" in package.__all__
    assert "VerifierVerdict" in package.__all__


def test_namespace_exports_failure_diagnosis_contract() -> None:
    assert "FailureDiagnosis" in package.__all__
    assert "FailureDiagnosisError" in package.__all__
    assert "FailureErrorCode" in package.__all__
    assert "FailureEvidenceStatus" in package.__all__
    assert "FailureType" in package.__all__


def test_namespace_exports_failure_pattern_contract() -> None:
    assert "FailurePatternMiningError" in package.__all__
    assert "FailurePatternMiningErrorCode" in package.__all__
    assert "FailurePatternMiningObservation" in package.__all__
    assert "FailurePatternMiningStatus" in package.__all__
    assert "FailurePatternOrdering" in package.__all__
    assert "FailurePatternSummary" in package.__all__
    assert "MineFailurePatternsInput" in package.__all__


def test_namespace_exports_failure_curation_contract() -> None:
    expected = {
        "DeferredFailure",
        "FailureCuration",
        "FailureCurationErrorCode",
        "FailureCurationFailure",
        "FailureGroup",
        "FailureGroupMember",
        "FailureSource",
    }

    assert expected <= set(package.__all__)


def test_namespace_exports_workspace_preparation_contract() -> None:
    assert "PreparationErrorCode" in package.__all__
    assert "PreparationPhase" in package.__all__
    assert "PreparationStatus" in package.__all__
    assert "PrepareWorkspaceInput" in package.__all__
    assert "WorkspacePreparationObservation" in package.__all__


def test_namespace_exports_policy_and_hypothesis_contracts_without_legacy_ownership() -> None:
    expected = {
        "ExperimentPolicySnapshot",
        "FailurePatternReference",
        "HarnessChangeTarget",
        "HarnessHypothesis",
        "HypothesisErrorCode",
        "HypothesisId",
        "HypothesisObservation",
        "RecordHypothesisInput",
    }

    assert expected <= set(package.__all__)
    assert {"AssetAccess", "HarnessRevision", "HarnessRevisionId"}.isdisjoint(package.__all__)


def test_namespace_exports_candidate_contracts_without_restoring_runtime_plane() -> None:
    expected = {
        "CandidateExecutionInput",
        "CandidateExecutionObservation",
        "CandidateId",
        "CandidatePhase",
        "CandidateStatus",
    }

    assert expected <= set(package.__all__)
    assert {"E2BSandbox", "CanaryCase", "CommandLoop"}.isdisjoint(package.__all__)
    assert {"CandidateBlocker", "CandidateOutcomeReceipt"}.isdisjoint(package.__all__)
