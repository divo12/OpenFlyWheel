"""Controlled AHE candidate worktree and manifest behavior."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from ofw import (
    CandidateBuilder,
    CandidateError,
    CandidateErrorCode,
    CandidateEvidence,
    CandidatePolicy,
    ChangePrediction,
    ClusterId,
    ComponentKind,
    FileEdit,
    Harness,
    LineRange,
    Sha256Digest,
    Tool,
    ofw,
)
from ofw.contracts import HarnessRevisionId


def _run_git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _harness(tmp_path: Path) -> Harness:
    root = tmp_path / "candidate-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Frozen prompt.\n", encoding="utf-8")
    (root / "tool.py").write_text(
        "def run(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )
    (root / "verifier.py").write_text("VERIFIER = 'frozen'\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    harness = Harness("candidate-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_tools(Tool("run", ofw.editable(Path("tool.py"))))
    harness.process()
    return harness


def _digest(path: Path) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}")


def _evidence(revision_id: HarnessRevisionId) -> CandidateEvidence:
    return CandidateEvidence(
        revision_id,
        (ClusterId("cluster-tool-schema"),),
        ("frontier-case", "regression-case"),
        (ClusterId("cluster-memory"),),
    )


def _prediction() -> ChangePrediction:
    return ChangePrediction(
        hypothesis="Normalize tool arguments before execution.",
        target_clusters=(ClusterId("cluster-tool-schema"),),
        at_risk_cases=("regression-case",),
        affected_components=(ComponentKind.TOOL,),
        memory_candidates=(ClusterId("cluster-memory"),),
        expected_quality_delta=0.1,
        expected_cost_delta=0.0,
        expected_latency_delta=0.0,
    )


def _policy() -> CandidatePolicy:
    return CandidatePolicy(
        maximum_files=2,
        maximum_changed_bytes=1024,
        allowed_components=(ComponentKind.TOOL,),
    )


def test_candidate_edits_only_tool_in_isolated_branch_and_preserves_manifest(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    tool = revision.root / "tool.py"
    edit = FileEdit(
        Path("tool.py"),
        _digest(tool),
        "def run(value: str) -> str:\n    return value.strip()\n",
    )
    builder = CandidateBuilder(revision, _evidence(revision.id), _policy())

    build = builder.create((edit,), _prediction())
    manifest_before = build.candidate.manifest_path.read_bytes()
    try:
        assert build.workspace.root != revision.root
        assert (build.workspace.root / "prompt.md").read_text(
            encoding="utf-8"
        ) == "Frozen prompt.\n"
        assert "strip" in (build.workspace.root / "tool.py").read_text(encoding="utf-8")
        assert build.candidate.changed_components == (ComponentKind.TOOL,)
        assert build.candidate.diff_path.is_file()
        assert build.candidate.manifest_path.read_bytes() == manifest_before
    finally:
        branch = build.workspace.branch
        root = build.workspace.root
        build.workspace.close()

    assert not root.exists()
    assert branch.value not in _run_git(revision.root, "branch", "--format=%(refname:short)")


@pytest.mark.parametrize("path", (Path("prompt.md"), Path("verifier.py"), Path(".env")))
def test_frozen_or_undeclared_file_edit_is_rejected(path: Path, tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    source = revision.root / path
    expected = _digest(source) if source.is_file() else Sha256Digest("sha256:missing")
    edit = FileEdit(path, expected, "changed\n")

    with pytest.raises(CandidateError) as raised:
        CandidateBuilder(revision, _evidence(revision.id), _policy()).create(
            (edit,),
            _prediction(),
        )

    assert raised.value.code is CandidateErrorCode.FILE_NOT_EDITABLE


def test_stale_base_digest_is_rejected_before_worktree_creation(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    edit = FileEdit(
        Path("tool.py"),
        Sha256Digest("sha256:stale"),
        "changed\n",
    )

    with pytest.raises(CandidateError) as raised:
        CandidateBuilder(revision, _evidence(revision.id), _policy()).create(
            (edit,),
            _prediction(),
        )

    assert raised.value.code is CandidateErrorCode.BASE_DIGEST_MISMATCH


def test_line_selector_changes_only_declared_range(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    tool = revision.root / "tool.py"
    edit = FileEdit(
        Path("tool.py"),
        _digest(tool),
        "    return value.strip()\n",
        selector=LineRange(2, 2),
    )

    build = CandidateBuilder(revision, _evidence(revision.id), _policy()).create(
        (edit,),
        _prediction(),
    )
    try:
        assert (build.workspace.root / "tool.py").read_text(encoding="utf-8") == (
            "def run(value: str) -> str:\n    return value.strip()\n"
        )
    finally:
        build.workspace.close()


def test_prediction_must_reference_known_evidence(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    unknown = ChangePrediction(
        hypothesis="Unknown target.",
        target_clusters=(ClusterId("cluster-unknown"),),
        at_risk_cases=(),
        affected_components=(ComponentKind.TOOL,),
        memory_candidates=(),
        expected_quality_delta=0.1,
        expected_cost_delta=0.0,
        expected_latency_delta=0.0,
    )

    with pytest.raises(CandidateError) as raised:
        CandidateBuilder(revision, _evidence(revision.id), _policy()).create((), unknown)

    assert raised.value.code is CandidateErrorCode.EVIDENCE_MISMATCH


def test_candidate_budget_limits_file_count_and_bytes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    tool = revision.root / "tool.py"
    oversized = FileEdit(Path("tool.py"), _digest(tool), "x" * 2048)

    with pytest.raises(CandidateError) as raised:
        CandidateBuilder(revision, _evidence(revision.id), _policy()).create(
            (oversized,),
            _prediction(),
        )

    assert raised.value.code is CandidateErrorCode.BUDGET_EXCEEDED


def test_noop_edit_is_rejected_and_worktree_is_cleaned(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    revision = harness.current_revision
    assert revision is not None
    tool = revision.root / "tool.py"
    branches_before = _run_git(revision.root, "branch", "--format=%(refname:short)")

    with pytest.raises(CandidateError) as raised:
        CandidateBuilder(revision, _evidence(revision.id), _policy()).create(
            (FileEdit(Path("tool.py"), _digest(tool), tool.read_text(encoding="utf-8")),),
            _prediction(),
        )

    assert raised.value.code is CandidateErrorCode.NO_CHANGES
    assert _run_git(revision.root, "branch", "--format=%(refname:short)") == branches_before
