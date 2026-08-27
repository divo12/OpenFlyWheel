"""Installed-package typing contract."""

from pathlib import Path

import ofw as package
from ofw import ofw


def test_package_declares_inline_types() -> None:
    package_file = package.__file__
    assert package_file is not None
    assert Path(package_file).with_name("py.typed").is_file()


def test_namespace_excludes_removed_collection_api() -> None:
    assert "collect" not in package.__all__
    assert "CollectionResult" not in package.__all__


def test_namespace_keeps_harness_methods() -> None:
    assert callable(ofw.editable)
    assert callable(ofw.E2BSandbox)
    assert callable(ofw.ProcessLimits)


def test_namespace_exports_authoritative_outcome_contract() -> None:
    assert "OutcomeEvaluation" in package.__all__
    assert "LangfuseOutcomeStore" in package.__all__
    assert "EvidenceReference" in package.__all__
    assert "TaskId" in package.__all__
    assert "TraceId" in package.__all__
    assert "VerifierId" in package.__all__


def test_namespace_exports_workspace_preparation_contract() -> None:
    assert "PreparationErrorCode" in package.__all__
    assert "PreparationPhase" in package.__all__
    assert "PreparationStatus" in package.__all__
    assert "PrepareWorkspaceInput" in package.__all__
    assert "WorkspacePreparationObservation" in package.__all__
