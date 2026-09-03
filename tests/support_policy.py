from __future__ import annotations

from pathlib import Path

from ofw.contracts import ComponentKind
from ofw.evolution.hypothesis import (
    HarnessChangeTarget,
    HarnessHypothesis,
    HypothesisId,
)
from ofw.preparation.policy import ExperimentPolicySnapshot


def policy(
    *, max_iterations: int = 2, no_improvement_limit: int = 1
) -> ExperimentPolicySnapshot:
    draft = ExperimentPolicySnapshot.model_construct(
        experiment_id="experiment-one",
        branch_name="ofw/experiment-one",
        base_commit="a" * 40,
        initialization_commit="a" * 40,
        editable_paths=(Path("PROGRAM.md"),),
        goal="Improve quality",
        quality_target=1.0,
        max_iterations=max_iterations,
        no_improvement_limit=no_improvement_limit,
        max_baseline_seconds=60,
        benchmark_config_digest="sha256:" + "b" * 64,
        task_ids=("task-1",),
        model="model",
        verifier="verifier",
        environment="test",
        controls_digest="sha256:" + "0" * 64,
    )
    # Pydantic's model_construct is the only way to bootstrap this self-hashed fixture.
    return draft.model_copy(
        update={"controls_digest": draft.recomputed_controls_digest()}  # type: ignore[misc]
    )


class PolicyRepository:
    def __init__(self, value: ExperimentPolicySnapshot) -> None:
        self.value = value

    def load(
        self, workspace_root: Path, experiment_id: str
    ) -> ExperimentPolicySnapshot:
        del workspace_root, experiment_id
        return self.value


class HypothesisRepository:
    def load(self, workspace_root: Path, hypothesis_id: str) -> HarnessHypothesis:
        del workspace_root
        return HarnessHypothesis(
            id=HypothesisId(hypothesis_id),
            experiment_id="experiment-one",
            source_commit="a" * 40,
            curation_id="00000000-0000-0000-0000-000000000001",
            curation_group_id="00000000-0000-0000-0000-000000000002",
            patterns=(),
            statement="statement",
            rationale="rationale",
            target=HarnessChangeTarget(ComponentKind.SKILL, (Path("PROGRAM.md"),)),
            expected_effect="effect",
            regression_risks=(),
        )
