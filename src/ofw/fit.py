"""Paired candidate evaluation, progressive gates, and one-shot admission."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from ofw.benchmarking import (
    Baseline,
    BenchmarkPolicy,
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkStatus,
    CaseAttempt,
)
from ofw.candidate import (
    CandidateBuild,
    CandidateError,
    CandidateId,
    CandidateManifest,
    read_candidate_manifest,
    validate_candidate_artifacts,
    validate_candidate_revision,
)
from ofw.contracts import HarnessRevision, HarnessRevisionId, Sha256Digest
from ofw.exports import (
    ClusterFamilyId,
    EvalCase,
    ExportBundle,
    ExportPartition,
    SnapshotReference,
    TraceFamilyId,
)
from ofw.harness import Harness
from ofw.mine import digest_bytes, write_artifact
from ofw.observability.langfuse.domain import TraceId
from ofw.runtime import MetricKind, RunResult, VerifierResult


class FitErrorCode(StrEnum):
    ADMISSION_ALREADY_USED = "admission_already_used"
    RESULT_INVALID = "result_invalid"
    CANDIDATE_DRIFT = "candidate_drift"


class FitError(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: FitErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


class CandidateStatus(StrEnum):
    REJECTED = "rejected"
    SURVIVED = "survived"
    WINNER = "winner"


class GateReason(StrEnum):
    PASSED = "passed"
    INCOMPLETE_RUN = "incomplete_run"
    CRITICAL_REGRESSION = "critical_regression"
    REGRESSION_SCORE = "regression_score"
    TARGET_DELTA = "target_delta"
    LATENCY = "latency"
    SELECTION = "selection"
    ADMISSION = "admission"
    PARETO = "pareto"
    COST = "cost"
    STATISTICAL_EVIDENCE = "statistical_evidence"


class StatisticalGateMode(StrEnum):
    EFFECT_SIZE_ONLY = "effect_size_only"
    EXACT_SIGN_TEST = "exact_sign_test"


class AdmissionState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class FitExperienceSchemaVersion(IntEnum):
    V1 = 1


class HoldoutStage(StrEnum):
    SELECTION = "selection"
    ADMISSION = "admission"


@dataclass(frozen=True, slots=True)
class PairedEvidencePolicy:
    mode: StatisticalGateMode
    minimum_discordant_pairs: int
    maximum_probability: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.mode, StatisticalGateMode)
            or self.minimum_discordant_pairs < 0
            or not math.isfinite(self.maximum_probability)
            or not 0 < self.maximum_probability <= 1
            or (
                self.mode is StatisticalGateMode.EFFECT_SIZE_ONLY
                and (self.minimum_discordant_pairs != 0 or self.maximum_probability != 1.0)
            )
            or (
                self.mode is StatisticalGateMode.EXACT_SIGN_TEST
                and self.minimum_discordant_pairs < 1
            )
        ):
            raise ValueError("invalid paired evidence policy")


@dataclass(frozen=True, slots=True)
class FitPolicy:
    minimum_target_delta: float
    minimum_regression_score: float
    maximum_critical_regressions: int
    maximum_latency_delta: float
    maximum_cost_delta: float
    minimum_selection_pass_rate: float
    minimum_admission_pass_rate: float
    paired_evidence_policy: PairedEvidencePolicy

    def __post_init__(self) -> None:
        values = (
            self.minimum_target_delta,
            self.minimum_regression_score,
            self.maximum_latency_delta,
            self.maximum_cost_delta,
            self.minimum_selection_pass_rate,
            self.minimum_admission_pass_rate,
        )
        if (
            not all(math.isfinite(value) for value in values)
            or self.maximum_critical_regressions < 0
        ):
            raise ValueError("invalid fit policy")

    @property
    def digest(self) -> Sha256Digest:
        return Sha256Digest(
            "sha256:"
            + hashlib.sha256(
                "\0".join(
                    (
                        str(self.minimum_target_delta),
                        str(self.minimum_regression_score),
                        str(self.maximum_critical_regressions),
                        str(self.maximum_latency_delta),
                        str(self.maximum_cost_delta),
                        str(self.minimum_selection_pass_rate),
                        str(self.minimum_admission_pass_rate),
                        self.paired_evidence_policy.mode.value,
                        str(self.paired_evidence_policy.minimum_discordant_pairs),
                        str(self.paired_evidence_policy.maximum_probability),
                    )
                ).encode()
            ).hexdigest()
        )


@dataclass(frozen=True, slots=True)
class CaseDelta:
    case_id: str
    partition: ExportPartition
    critical: bool
    synthetic: bool
    weight: float
    baseline_passed: bool
    candidate_passed: bool
    pass_delta: int
    score_delta: float
    latency_delta: float
    cost_delta: float


@dataclass(frozen=True, slots=True)
class PairedEvidence:
    partition: ExportPartition
    wins: int
    losses: int
    ties: int
    discordant_pairs: int
    net_pass_delta: float
    candidate_win_rate: float
    exact_one_sided_probability: float


@dataclass(frozen=True, slots=True)
class ManifestAttribution:
    predicted_quality_delta: float
    actual_quality_delta: float
    prediction_error: float
    predicted_cost_delta: float
    actual_cost_delta: float
    cost_prediction_error: float
    predicted_latency_delta: float
    actual_latency_delta: float
    latency_prediction_error: float


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    candidate_id: CandidateId
    status: CandidateStatus
    reason: GateReason
    developer_result: BenchmarkResult
    deltas: tuple[CaseDelta, ...]
    paired_evidence: tuple[PairedEvidence, ...]
    critical_regressions: int
    target_delta: float
    regression_score: float
    latency_delta: float
    cost_delta: float
    attribution: ManifestAttribution
    selection_result: BenchmarkResult | None = None
    admission_result: BenchmarkResult | None = None


@dataclass(frozen=True, slots=True)
class FitArtifactReference:
    path: Path
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class PairedAttemptExperience:
    case_id: str
    repeat: int
    synthetic: bool
    weight: float
    baseline: RunResult
    candidate: RunResult
    baseline_verifiers: tuple[VerifierResult, ...]
    candidate_verifiers: tuple[VerifierResult, ...]
    delta: CaseDelta


@dataclass(frozen=True, slots=True)
class DeveloperCaseExperience:
    case_id: str
    trace_id: TraceId
    family_id: TraceFamilyId
    cluster_family_id: ClusterFamilyId
    partition: ExportPartition
    snapshot: SnapshotReference
    attempts: tuple[PairedAttemptExperience, ...]


@dataclass(frozen=True, slots=True)
class HoldoutDecision:
    stage: HoldoutStage
    status: BenchmarkStatus
    passed: bool


@dataclass(frozen=True, slots=True)
class CandidateExperience:
    candidate_id: CandidateId
    status: CandidateStatus
    reason: GateReason
    manifest: CandidateManifest
    attribution: ManifestAttribution
    candidate_diff: FitArtifactReference
    developer_baseline: FitArtifactReference
    developer_candidate: FitArtifactReference
    developer_cases: tuple[DeveloperCaseExperience, ...]
    holdouts: tuple[HoldoutDecision, ...]


@dataclass(frozen=True, slots=True)
class FitExperience:
    schema_version: FitExperienceSchemaVersion
    fit_id: str
    export_bundle_id: str
    input_digest: Sha256Digest
    revision_id: HarnessRevisionId
    candidates: tuple[CandidateExperience, ...]
    root: Path

    @property
    def path(self) -> Path:
        return self.root / ".ofw" / "fit" / self.fit_id / "experience.json"

    def to_json(self) -> str:
        return _EXPERIENCE_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class FitResult:
    id: str
    export_bundle_id: str
    benchmark_id: str
    policy_digest: Sha256Digest
    input_digest: Sha256Digest
    baseline: Baseline
    outcomes: tuple[CandidateOutcome, ...]
    winner_id: CandidateId | None
    experience_digest: Sha256Digest
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "fit" / self.id / "manifest.json"

    @property
    def digest_path(self) -> Path:
        return self.manifest_path.with_suffix(".sha256")

    @property
    def experience_path(self) -> Path:
        return self.manifest_path.with_name("experience.json")

    def to_json(self) -> str:
        return _FIT_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class _Survivor:
    build: CandidateBuild
    outcome: CandidateOutcome


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    campaign_id: str
    candidate_id: CandidateId
    state: AdmissionState
    result_path: Path | None = None
    semantic_digest: Sha256Digest | None = None

    def to_json(self) -> str:
        return _ADMISSION_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class CandidateInputFingerprint:
    candidate_id: CandidateId
    artifact_digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class SnapshotInputFingerprint:
    case_id: str
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class FitInputFingerprint:
    revision_id: str
    revision_manifest_digest: Sha256Digest
    bundle_digest: Sha256Digest
    benchmark_policy_digest: Sha256Digest
    fit_policy_digest: Sha256Digest
    candidates: tuple[CandidateInputFingerprint, ...]
    snapshots: tuple[SnapshotInputFingerprint, ...]

    @property
    def digest(self) -> Sha256Digest:
        return digest_bytes(_INPUT_ADAPTER.dump_json(self))


_FIT_ADAPTER: TypeAdapter[FitResult] = TypeAdapter(FitResult)
_EXPERIENCE_ADAPTER: TypeAdapter[FitExperience] = TypeAdapter(FitExperience)
_ADMISSION_ADAPTER: TypeAdapter[AdmissionRecord] = TypeAdapter(AdmissionRecord)
_DIGEST_ADAPTER: TypeAdapter[Sha256Digest] = TypeAdapter(Sha256Digest)
_INPUT_ADAPTER: TypeAdapter[FitInputFingerprint] = TypeAdapter(FitInputFingerprint)


@dataclass(frozen=True, slots=True)
class FitCampaign:
    harness: Harness
    bundle: ExportBundle
    benchmark_policy: BenchmarkPolicy
    fit_policy: FitPolicy
    candidates: tuple[CandidateBuild, ...]

    def wait(self) -> FitResult:
        return self.run()

    def run(self) -> FitResult:
        existing = self._read_existing()
        if existing is not None:
            return existing
        try:
            return self._run()
        except Exception:
            for candidate in self.candidates:
                candidate.workspace.close()
            raise

    def _run(self) -> FitResult:
        if not self.candidates:
            raise ValueError("fit campaign requires candidates")
        champion_revision = self.harness.current_revision
        if champion_revision is None:
            raise FitError(FitErrorCode.CANDIDATE_DRIFT, self.harness.name)
        input_digest = self._validate_inputs(champion_revision)
        runner = BenchmarkRunner(self.harness, self.bundle, self.benchmark_policy)
        baseline = runner.establish_baseline()
        champion = runner.verify_baseline(baseline)
        outcomes: tuple[CandidateOutcome, ...] = ()
        survivors: tuple[_Survivor, ...] = ()
        for build in self.candidates:
            candidate_result = runner.run_candidate(build.candidate)
            outcome = _developer_outcome(
                build,
                champion,
                candidate_result,
                self.fit_policy,
                len(self.candidates),
            )
            outcomes = (*outcomes, outcome)
            if outcome.status is CandidateStatus.SURVIVED:
                survivors = (*survivors, _Survivor(build, outcome))
            else:
                build.workspace.close()
        selected: tuple[_Survivor, ...] = ()
        for survivor in survivors:
            selection = runner.run_selection(survivor.build.candidate)
            if (
                selection.status is BenchmarkStatus.COMPLETE
                and selection.weighted_pass_rate >= self.fit_policy.minimum_selection_pass_rate
            ):
                updated = replace(survivor.outcome, selection_result=selection)
                outcomes = _replace_outcome(outcomes, updated)
                selected = (*selected, _Survivor(survivor.build, updated))
            else:
                rejected = replace(
                    survivor.outcome,
                    status=CandidateStatus.REJECTED,
                    reason=GateReason.SELECTION,
                    selection_result=selection,
                )
                outcomes = _replace_outcome(outcomes, rejected)
                survivor.build.workspace.close()
        finalist = _select_finalist(selected)
        for survivor in selected:
            if finalist is None or survivor.build.candidate.id != finalist.build.candidate.id:
                rejected = replace(
                    survivor.outcome,
                    status=CandidateStatus.REJECTED,
                    reason=GateReason.PARETO,
                )
                outcomes = _replace_outcome(outcomes, rejected)
                survivor.build.workspace.close()
        winner_id: CandidateId | None = None
        if finalist is not None:
            record_path = self._admission_record_path(finalist.build.candidate.id)
            if record_path.exists():
                read_admission_record(record_path)
                raise FitError(
                    FitErrorCode.ADMISSION_ALREADY_USED, finalist.build.candidate.id.value
                )
            record = AdmissionRecord(
                self._campaign_id(),
                finalist.build.candidate.id,
                AdmissionState.RUNNING,
            )
            write_artifact(record_path, f"{record.to_json()}\n".encode())
            try:
                admission = runner.run_admission(finalist.build.candidate)
            except Exception:
                failed = replace(record, state=AdmissionState.ERROR)
                write_artifact(record_path, f"{failed.to_json()}\n".encode())
                raise
            completed = replace(
                record,
                state=AdmissionState.COMPLETED,
                result_path=admission.manifest_path,
                semantic_digest=admission.semantic_digest,
            )
            write_artifact(record_path, f"{completed.to_json()}\n".encode())
            if (
                admission.status is BenchmarkStatus.COMPLETE
                and admission.weighted_pass_rate >= self.fit_policy.minimum_admission_pass_rate
            ):
                winner = replace(
                    finalist.outcome,
                    status=CandidateStatus.WINNER,
                    reason=GateReason.PASSED,
                    admission_result=admission,
                )
                winner_id = finalist.build.candidate.id
                outcomes = _replace_outcome(outcomes, winner)
            else:
                rejected = replace(
                    finalist.outcome,
                    status=CandidateStatus.REJECTED,
                    reason=GateReason.ADMISSION,
                    admission_result=admission,
                )
                outcomes = _replace_outcome(outcomes, rejected)
                finalist.build.workspace.close()
        experience = _fit_experience(
            self._campaign_id(),
            self.bundle,
            input_digest,
            champion,
            self.candidates,
            outcomes,
            self.fit_policy,
        )
        experience_payload = f"{experience.to_json()}\n".encode()
        write_artifact(experience.path, experience_payload)
        result = FitResult(
            self._campaign_id(),
            self.bundle.id,
            baseline.benchmark_id,
            self.fit_policy.digest,
            input_digest,
            baseline,
            outcomes,
            winner_id,
            digest_bytes(experience_payload),
            self.harness.root,
        )
        payload = f"{result.to_json()}\n".encode()
        write_artifact(result.manifest_path, payload)
        write_artifact(result.digest_path, _DIGEST_ADAPTER.dump_json(digest_bytes(payload)) + b"\n")
        return result

    def _campaign_id(self) -> str:
        return (
            "fit_"
            + hashlib.sha256(
                "\0".join(
                    (
                        self.bundle.id,
                        str(self.benchmark_policy.digest),
                        str(self.fit_policy.digest),
                        *(candidate.candidate.id.value for candidate in self.candidates),
                    )
                ).encode()
            ).hexdigest()
        )

    def _admission_record_path(self, candidate_id: CandidateId) -> Path:
        return (
            self.harness.root
            / ".ofw"
            / "fit"
            / self._campaign_id()
            / f"admission-{candidate_id.value}.json"
        )

    def _read_existing(self) -> FitResult | None:
        path = self.harness.root / ".ofw" / "fit" / self._campaign_id() / "manifest.json"
        if not path.exists():
            return None
        try:
            payload = path.read_bytes()
            expected = _DIGEST_ADAPTER.validate_json(path.with_suffix(".sha256").read_bytes())
            result = _FIT_ADAPTER.validate_json(payload)
        except (OSError, ValidationError) as error:
            raise FitError(FitErrorCode.RESULT_INVALID, str(path)) from error
        if digest_bytes(payload) != expected:
            raise FitError(FitErrorCode.RESULT_INVALID, str(path))
        candidate_ids = tuple(candidate.candidate.id for candidate in self.candidates)
        champion_revision = self.harness.current_revision
        if champion_revision is None:
            raise FitError(FitErrorCode.CANDIDATE_DRIFT, self.harness.name)
        input_digest = self._validate_inputs(champion_revision)
        if (
            result.id != self._campaign_id()
            or result.export_bundle_id != self.bundle.id
            or result.benchmark_id != self.bundle.benchmark.id
            or result.policy_digest != self.fit_policy.digest
            or result.input_digest != input_digest
            or tuple(outcome.candidate_id for outcome in result.outcomes) != candidate_ids
            or (result.winner_id is not None and result.winner_id not in candidate_ids)
        ):
            raise FitError(FitErrorCode.RESULT_INVALID, str(path))
        if result.winner_id is not None:
            winner = next(
                candidate
                for candidate in self.candidates
                if candidate.candidate.id == result.winner_id
            )
            if not winner.workspace.root.exists():
                raise FitError(FitErrorCode.CANDIDATE_DRIFT, winner.candidate.id.value)
        read_fit_experience(result)
        return result

    def _validate_inputs(self, champion_revision: HarnessRevision) -> Sha256Digest:
        try:
            revision_manifest_digest = digest_bytes(champion_revision.manifest_path.read_bytes())
            candidate_fingerprints = tuple(
                CandidateInputFingerprint(
                    build.candidate.id,
                    validate_candidate_artifacts(build.candidate, champion_revision),
                )
                for build in self.candidates
            )
            for build in self.candidates:
                if build.workspace.root.exists():
                    validate_candidate_revision(build.candidate, champion_revision)
        except CandidateError as error:
            raise FitError(FitErrorCode.CANDIDATE_DRIFT, error.subject) from error
        except OSError as error:
            raise FitError(
                FitErrorCode.RESULT_INVALID,
                str(champion_revision.manifest_path),
            ) from error
        snapshot_fingerprints = tuple(
            _snapshot_fingerprint(
                case.id,
                case.snapshot.path,
                case.snapshot.digest,
                champion_revision,
            )
            for suite in (
                self.bundle.developer_evals,
                self.bundle.selection_holdout,
                self.bundle.admission_holdout,
            )
            for case in suite.cases
        )
        return FitInputFingerprint(
            str(champion_revision.id),
            revision_manifest_digest,
            digest_bytes(self.bundle.to_json().encode()),
            self.benchmark_policy.digest,
            self.fit_policy.digest,
            candidate_fingerprints,
            snapshot_fingerprints,
        ).digest


def read_fit_experience(result: FitResult) -> FitExperience:
    try:
        payload = result.experience_path.read_bytes()
        experience = _EXPERIENCE_ADAPTER.validate_json(payload)
    except (OSError, ValidationError) as error:
        raise FitError(FitErrorCode.RESULT_INVALID, str(result.experience_path)) from error
    outcome_ids = tuple(outcome.candidate_id for outcome in result.outcomes)
    if (
        digest_bytes(payload) != result.experience_digest
        or experience.fit_id != result.id
        or experience.export_bundle_id != result.export_bundle_id
        or experience.input_digest != result.input_digest
        or experience.revision_id != result.baseline.revision_id
        or tuple(candidate.candidate_id for candidate in experience.candidates) != outcome_ids
    ):
        raise FitError(FitErrorCode.RESULT_INVALID, str(result.experience_path))
    for candidate, outcome in zip(experience.candidates, result.outcomes, strict=True):
        if (
            candidate.status is not outcome.status
            or candidate.reason is not outcome.reason
            or candidate.attribution != outcome.attribution
            or candidate.developer_candidate.path != outcome.developer_result.manifest_path
        ):
            raise FitError(FitErrorCode.RESULT_INVALID, candidate.candidate_id.value)
        _validate_experience_artifacts(result.root, candidate)
    return experience


def _fit_experience(
    fit_id: str,
    bundle: ExportBundle,
    input_digest: Sha256Digest,
    baseline: BenchmarkResult,
    builds: tuple[CandidateBuild, ...],
    outcomes: tuple[CandidateOutcome, ...],
    policy: FitPolicy,
) -> FitExperience:
    candidates = tuple(
        _candidate_experience(
            build,
            next(outcome for outcome in outcomes if outcome.candidate_id == build.candidate.id),
            bundle.developer_evals.cases,
            baseline,
            policy,
        )
        for build in builds
    )
    return FitExperience(
        FitExperienceSchemaVersion.V1,
        fit_id,
        bundle.id,
        input_digest,
        bundle.revision_id,
        candidates,
        bundle.root,
    )


def _candidate_experience(
    build: CandidateBuild,
    outcome: CandidateOutcome,
    cases: tuple[EvalCase, ...],
    baseline: BenchmarkResult,
    policy: FitPolicy,
) -> CandidateExperience:
    return CandidateExperience(
        outcome.candidate_id,
        outcome.status,
        outcome.reason,
        read_candidate_manifest(build.candidate.manifest_path),
        outcome.attribution,
        _artifact_reference(build.candidate.diff_path),
        _artifact_reference(baseline.manifest_path),
        _artifact_reference(outcome.developer_result.manifest_path),
        tuple(
            _developer_case_experience(case, baseline, outcome.developer_result)
            for case in cases
        ),
        _holdout_decisions(outcome, policy),
    )


def _developer_case_experience(
    case: EvalCase,
    baseline: BenchmarkResult,
    candidate: BenchmarkResult,
) -> DeveloperCaseExperience:
    attempts = tuple(
        _paired_attempt_experience(baseline_attempt, candidate_attempt)
        for baseline_attempt in baseline.attempts
        for candidate_attempt in candidate.attempts
        if baseline_attempt.source_case_id == case.id
        and candidate_attempt.source_case_id == case.id
        and _attempt_key(baseline_attempt) == _attempt_key(candidate_attempt)
    )
    return DeveloperCaseExperience(
        case.id,
        case.trace_id,
        case.family_id,
        case.cluster_family_id,
        case.partition,
        case.snapshot,
        attempts,
    )


def _paired_attempt_experience(
    baseline: CaseAttempt,
    candidate: CaseAttempt,
) -> PairedAttemptExperience:
    return PairedAttemptExperience(
        baseline.case_id,
        baseline.repeat,
        baseline.synthetic,
        baseline.weight,
        baseline.run,
        candidate.run,
        baseline.verifiers,
        candidate.verifiers,
        _case_delta(baseline, candidate),
    )


def _holdout_decisions(
    outcome: CandidateOutcome,
    policy: FitPolicy,
) -> tuple[HoldoutDecision, ...]:
    decisions: tuple[HoldoutDecision, ...] = ()
    if outcome.selection_result is not None:
        decisions = (
            HoldoutDecision(
                HoldoutStage.SELECTION,
                outcome.selection_result.status,
                outcome.selection_result.status is BenchmarkStatus.COMPLETE
                and outcome.selection_result.weighted_pass_rate
                >= policy.minimum_selection_pass_rate,
            ),
        )
    if outcome.admission_result is not None:
        decisions = (
            *decisions,
            HoldoutDecision(
                HoldoutStage.ADMISSION,
                outcome.admission_result.status,
                outcome.admission_result.status is BenchmarkStatus.COMPLETE
                and outcome.admission_result.weighted_pass_rate
                >= policy.minimum_admission_pass_rate,
            ),
        )
    return decisions


def _artifact_reference(path: Path) -> FitArtifactReference:
    try:
        return FitArtifactReference(path, digest_bytes(path.read_bytes()))
    except OSError as error:
        raise FitError(FitErrorCode.RESULT_INVALID, str(path)) from error


def _validate_experience_artifacts(root: Path, candidate: CandidateExperience) -> None:
    references = (
        candidate.candidate_diff,
        candidate.developer_baseline,
        candidate.developer_candidate,
    )
    try:
        allowed = (root / ".ofw").resolve(strict=True)
        for reference in references:
            path = reference.path.resolve(strict=True)
            path.relative_to(allowed)
            if digest_bytes(path.read_bytes()) != reference.digest:
                raise FitError(FitErrorCode.RESULT_INVALID, str(path))
    except (OSError, ValueError) as error:
        raise FitError(FitErrorCode.RESULT_INVALID, candidate.candidate_id.value) from error


def read_admission_record(path: Path) -> AdmissionRecord:
    try:
        return _ADMISSION_ADAPTER.validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise FitError(FitErrorCode.RESULT_INVALID, str(path)) from error


def _snapshot_fingerprint(
    case_id: str,
    path: Path,
    expected: Sha256Digest,
    revision: HarnessRevision,
) -> SnapshotInputFingerprint:
    try:
        allowed = (revision.root / ".ofw").resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(allowed)
        actual = digest_bytes(resolved.read_bytes())
    except (OSError, ValueError) as error:
        raise FitError(FitErrorCode.RESULT_INVALID, case_id) from error
    if actual != expected:
        raise FitError(FitErrorCode.RESULT_INVALID, case_id)
    return SnapshotInputFingerprint(case_id, actual)


def _developer_outcome(
    build: CandidateBuild,
    baseline: BenchmarkResult,
    candidate: BenchmarkResult,
    policy: FitPolicy,
    comparison_count: int,
) -> CandidateOutcome:
    deltas = _case_deltas(baseline, candidate)
    evidence = paired_evidence(deltas)
    critical_regressions = sum(
        delta.critical
        and not delta.synthetic
        and delta.baseline_passed
        and not delta.candidate_passed
        for delta in deltas
    )
    target = _weighted_average(
        tuple(
            (float(delta.pass_delta), delta.weight)
            for delta in deltas
            if delta.partition is ExportPartition.FRONTIER
        )
    )
    regression = _weighted_average(
        tuple(
            (float(delta.candidate_passed), delta.weight)
            for delta in deltas
            if delta.partition is ExportPartition.REGRESSION
        )
    )
    latency = _weighted_average(tuple((delta.latency_delta, delta.weight) for delta in deltas))
    cost = _weighted_average(tuple((delta.cost_delta, delta.weight) for delta in deltas))
    manifest = read_candidate_manifest(build.candidate.manifest_path)
    attribution = ManifestAttribution(
        manifest.expected_quality_delta,
        target,
        abs(manifest.expected_quality_delta - target),
        manifest.expected_cost_delta,
        cost,
        abs(manifest.expected_cost_delta - cost),
        manifest.expected_latency_delta,
        latency,
        abs(manifest.expected_latency_delta - latency),
    )
    reason = _developer_gate(
        candidate,
        critical_regressions,
        target,
        regression,
        latency,
        cost,
        evidence,
        comparison_count,
        policy,
    )
    status = CandidateStatus.SURVIVED if reason is GateReason.PASSED else CandidateStatus.REJECTED
    return CandidateOutcome(
        build.candidate.id,
        status,
        reason,
        candidate,
        deltas,
        evidence,
        critical_regressions,
        target,
        regression,
        latency,
        cost,
        attribution,
    )


def _developer_gate(
    result: BenchmarkResult,
    critical_regressions: int,
    target_delta: float,
    regression_score: float,
    latency_delta: float,
    cost_delta: float,
    evidence: tuple[PairedEvidence, ...],
    comparison_count: int,
    policy: FitPolicy,
) -> GateReason:
    if result.status is not BenchmarkStatus.COMPLETE:
        return GateReason.INCOMPLETE_RUN
    if critical_regressions > policy.maximum_critical_regressions:
        return GateReason.CRITICAL_REGRESSION
    if regression_score < policy.minimum_regression_score:
        return GateReason.REGRESSION_SCORE
    if target_delta < policy.minimum_target_delta:
        return GateReason.TARGET_DELTA
    frontier = next(
        (item for item in evidence if item.partition is ExportPartition.FRONTIER),
        None,
    )
    if policy.paired_evidence_policy.mode is StatisticalGateMode.EXACT_SIGN_TEST and (
        frontier is None
        or not paired_evidence_passes(
            frontier,
            comparison_count,
            policy.paired_evidence_policy,
        )
    ):
        return GateReason.STATISTICAL_EVIDENCE
    if latency_delta > policy.maximum_latency_delta:
        return GateReason.LATENCY
    if cost_delta > policy.maximum_cost_delta:
        return GateReason.COST
    return GateReason.PASSED


def paired_evidence(deltas: tuple[CaseDelta, ...]) -> tuple[PairedEvidence, ...]:
    real = tuple(delta for delta in deltas if not delta.synthetic)
    partitions = tuple(
        partition
        for partition in ExportPartition
        if any(delta.partition is partition for delta in real)
    )
    return tuple(_partition_evidence(partition, real) for partition in partitions)


def paired_evidence_passes(
    evidence: PairedEvidence,
    comparison_count: int,
    policy: PairedEvidencePolicy,
) -> bool:
    if comparison_count < 1:
        raise ValueError("comparison count must be positive")
    if policy.mode is StatisticalGateMode.EFFECT_SIZE_ONLY:
        return True
    return (
        evidence.discordant_pairs >= policy.minimum_discordant_pairs
        and evidence.wins > evidence.losses
        and evidence.exact_one_sided_probability <= policy.maximum_probability / comparison_count
    )


def _partition_evidence(
    partition: ExportPartition,
    deltas: tuple[CaseDelta, ...],
) -> PairedEvidence:
    selected = tuple(delta for delta in deltas if delta.partition is partition)
    wins = sum(delta.candidate_passed and not delta.baseline_passed for delta in selected)
    losses = sum(delta.baseline_passed and not delta.candidate_passed for delta in selected)
    ties = len(selected) - wins - losses
    discordant = wins + losses
    probability = _exact_one_sided_probability(wins, discordant)
    return PairedEvidence(
        partition,
        wins,
        losses,
        ties,
        discordant,
        0.0 if not selected else (wins - losses) / len(selected),
        0.5 if discordant == 0 else wins / discordant,
        probability,
    )


def _exact_one_sided_probability(wins: int, discordant_pairs: int) -> float:
    if discordant_pairs == 0:
        return 1.0
    numerator = sum(
        math.comb(discordant_pairs, successes) for successes in range(wins, discordant_pairs + 1)
    )
    return numerator / (1 << discordant_pairs)


def _case_deltas(
    baseline: BenchmarkResult,
    candidate: BenchmarkResult,
) -> tuple[CaseDelta, ...]:
    return tuple(
        _case_delta(baseline_attempt, candidate_attempt)
        for baseline_attempt in baseline.attempts
        for candidate_attempt in candidate.attempts
        if _attempt_key(baseline_attempt) == _attempt_key(candidate_attempt)
    )


def _case_delta(baseline: CaseAttempt, candidate: CaseAttempt) -> CaseDelta:
    baseline_score = _attempt_score(baseline.verifiers)
    candidate_score = _attempt_score(candidate.verifiers)
    baseline_latency = baseline.run.duration_seconds
    candidate_latency = candidate.run.duration_seconds
    baseline_cost = _attempt_cost(baseline.verifiers)
    candidate_cost = _attempt_cost(candidate.verifiers)
    latency_delta = (
        0.0 if baseline_latency == 0 else (candidate_latency - baseline_latency) / baseline_latency
    )
    return CaseDelta(
        baseline.case_id,
        baseline.partition,
        baseline.critical,
        baseline.synthetic,
        baseline.weight,
        baseline.passed,
        candidate.passed,
        int(candidate.passed) - int(baseline.passed),
        candidate_score - baseline_score,
        latency_delta,
        candidate_cost - baseline_cost,
    )


def _attempt_score(verifiers: tuple[VerifierResult, ...]) -> float:
    scores = tuple(verifier.score for verifier in verifiers if verifier.score is not None)
    return _average(scores)


def _attempt_cost(verifiers: tuple[VerifierResult, ...]) -> float:
    return sum(
        metric.value
        for verifier in verifiers
        for metric in verifier.metrics
        if metric.kind is MetricKind.COST_USD
    )


def _attempt_key(attempt: CaseAttempt) -> tuple[str, int, bool]:
    return attempt.case_id, attempt.repeat, attempt.synthetic


def _average(values: tuple[float | int, ...]) -> float:
    return 0.0 if not values else sum(values) / len(values)


def _weighted_average(values: tuple[tuple[float, float], ...]) -> float:
    weight = sum(item_weight for _, item_weight in values)
    return (
        0.0 if weight == 0 else sum(value * item_weight for value, item_weight in values) / weight
    )


def _replace_outcome(
    outcomes: tuple[CandidateOutcome, ...],
    replacement: CandidateOutcome,
) -> tuple[CandidateOutcome, ...]:
    return tuple(
        replacement if outcome.candidate_id == replacement.candidate_id else outcome
        for outcome in outcomes
    )


def _select_finalist(survivors: tuple[_Survivor, ...]) -> _Survivor | None:
    if not survivors:
        return None
    return sorted(survivors, key=_survivor_sort_key)[0]


def _survivor_sort_key(survivor: _Survivor) -> tuple[float, float, int]:
    selection = survivor.outcome.selection_result
    selection_score = 0.0 if selection is None else selection.weighted_pass_rate
    return (
        -selection_score,
        -survivor.outcome.target_delta,
        survivor.build.candidate.diff_path.stat().st_size,
    )
