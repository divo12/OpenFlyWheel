"""Paired candidate evaluation, progressive gates, and one-shot admission."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from enum import StrEnum
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
    read_candidate_manifest,
    validate_candidate_artifacts,
    validate_candidate_revision,
)
from ofw.contracts import HarnessRevision, Sha256Digest
from ofw.exports import ExportBundle, ExportPartition
from ofw.harness import Harness
from ofw.mine import digest_bytes, write_artifact
from ofw.runtime import MetricKind, VerifierResult


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


class AdmissionState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class FitPolicy:
    minimum_target_delta: float
    minimum_regression_score: float
    maximum_critical_regressions: int
    maximum_latency_delta: float
    maximum_cost_delta: float
    minimum_selection_pass_rate: float
    minimum_admission_pass_rate: float

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
    critical_regressions: int
    target_delta: float
    regression_score: float
    latency_delta: float
    cost_delta: float
    attribution: ManifestAttribution
    selection_result: BenchmarkResult | None = None
    admission_result: BenchmarkResult | None = None


@dataclass(frozen=True, slots=True)
class FitResult:
    id: str
    benchmark_id: str
    policy_digest: Sha256Digest
    input_digest: Sha256Digest
    baseline: Baseline
    outcomes: tuple[CandidateOutcome, ...]
    winner_id: CandidateId | None
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "fit" / self.id / "manifest.json"

    @property
    def digest_path(self) -> Path:
        return self.manifest_path.with_suffix(".sha256")

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
        result = FitResult(
            self._campaign_id(),
            baseline.benchmark_id,
            self.fit_policy.digest,
            input_digest,
            baseline,
            outcomes,
            winner_id,
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
        return result

    def _validate_inputs(self, champion_revision: HarnessRevision) -> Sha256Digest:
        try:
            revision_manifest_digest = digest_bytes(
                champion_revision.manifest_path.read_bytes()
            )
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
) -> CandidateOutcome:
    deltas = _case_deltas(baseline, candidate)
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
        policy,
    )
    status = CandidateStatus.SURVIVED if reason is GateReason.PASSED else CandidateStatus.REJECTED
    return CandidateOutcome(
        build.candidate.id,
        status,
        reason,
        candidate,
        deltas,
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
    if latency_delta > policy.maximum_latency_delta:
        return GateReason.LATENCY
    if cost_delta > policy.maximum_cost_delta:
        return GateReason.COST
    return GateReason.PASSED


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
