"""Reproducible benchmark runner with sealed holdouts and hard budgets."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import TypeAdapter

from ofw.contracts import HarnessRevision, HarnessRevisionId, Sha256Digest
from ofw.exports import EvalCase, ExportBundle, ExportPartition
from ofw.harness import Harness
from ofw.mine import digest_bytes, write_artifact
from ofw.runtime import (
    CanaryCase,
    CaseId,
    RunResult,
    RunStatus,
    VerifierResult,
    VerifierVerdict,
)


class BenchmarkStatus(StrEnum):
    COMPLETE = "complete"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ENVIRONMENT_ERROR = "environment_error"


class BenchmarkErrorCode(StrEnum):
    STALE_HARNESS = "stale_harness"
    REVISION_MISMATCH = "revision_mismatch"
    RUNTIME_MISMATCH = "runtime_mismatch"
    SNAPSHOT_INVALID = "snapshot_invalid"
    BASELINE_DRIFT = "baseline_drift"
    BASELINE_INCOMPLETE = "baseline_incomplete"
    HOLDOUT_LEAK = "holdout_leak"


class BenchmarkError(Exception):
    __slots__ = ("code", "subject")

    def __init__(self, code: BenchmarkErrorCode, subject: str) -> None:
        self.code = code
        self.subject = subject
        super().__init__(f"{code.value}: {subject}")


@dataclass(frozen=True, slots=True)
class BenchmarkPolicy:
    repeats: int
    max_attempts: int
    simulation_copies: int
    synthetic_weight: float

    def __post_init__(self) -> None:
        if (
            self.repeats < 1
            or self.max_attempts < 1
            or self.simulation_copies < 0
            or not math.isfinite(self.synthetic_weight)
            or self.synthetic_weight <= 0
            or self.synthetic_weight > 1
        ):
            raise BenchmarkError(BenchmarkErrorCode.BASELINE_INCOMPLETE, "invalid policy")

    @property
    def digest(self) -> Sha256Digest:
        return _digest_text(
            f"{self.repeats}\0{self.max_attempts}\0{self.simulation_copies}\0"
            f"{self.synthetic_weight}"
        )


@dataclass(frozen=True, slots=True)
class CaseAttempt:
    case_id: str
    repeat: int
    synthetic: bool
    weight: float
    run: RunResult
    verifiers: tuple[VerifierResult, ...]

    @property
    def passed(self) -> bool:
        return self.run.status is RunStatus.SUCCESS and all(
            verifier.verdict is VerifierVerdict.PASS for verifier in self.verifiers
        )


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    id: str
    benchmark_id: str
    revision_id: HarnessRevisionId
    policy_digest: Sha256Digest
    status: BenchmarkStatus
    attempts: tuple[CaseAttempt, ...]
    semantic_digest: Sha256Digest
    root: Path

    @property
    def weighted_pass_rate(self) -> float:
        total = sum(attempt.weight for attempt in self.attempts)
        if total == 0:
            return 0.0
        passed = sum(attempt.weight for attempt in self.attempts if attempt.passed)
        return passed / total

    @property
    def manifest_path(self) -> Path:
        return self.root / ".ofw" / "benchmarks" / self.benchmark_id / f"{self.id}.json"

    def to_json(self) -> str:
        return _RESULT_ADAPTER.dump_json(self).decode()


@dataclass(frozen=True, slots=True)
class Baseline:
    benchmark_id: str
    revision_id: HarnessRevisionId
    policy_digest: Sha256Digest
    semantic_digest: Sha256Digest
    path: Path

    def to_json(self) -> str:
        return _BASELINE_ADAPTER.dump_json(self).decode()


_ATTEMPTS_ADAPTER: TypeAdapter[tuple[CaseAttempt, ...]] = TypeAdapter(tuple[CaseAttempt, ...])
_RESULT_ADAPTER: TypeAdapter[BenchmarkResult] = TypeAdapter(BenchmarkResult)
_BASELINE_ADAPTER: TypeAdapter[Baseline] = TypeAdapter(Baseline)


@dataclass(frozen=True, slots=True)
class BenchmarkRunner:
    harness: Harness
    bundle: ExportBundle
    policy: BenchmarkPolicy

    def run(self) -> BenchmarkResult:
        revision = self._revision()
        execution, lifecycle, verifiers = self.harness.runtime_adapters()
        cases = self.bundle.developer_evals.cases
        if any(
            case.partition not in (ExportPartition.FRONTIER, ExportPartition.REGRESSION)
            or not _ledger_authorizes(case, self.bundle)
            for case in cases
        ):
            raise BenchmarkError(BenchmarkErrorCode.HOLDOUT_LEAK, self.bundle.developer_evals.id)
        attempts: list[CaseAttempt] = []
        status = BenchmarkStatus.COMPLETE
        if cases:
            prepared = execution.prepare(
                revision,
                CanaryCase(CaseId("benchmark"), ""),
            )
            try:
                for case in cases:
                    payload = _case_payload(case, revision.root)
                    variants = ((0, False, 1.0, payload),) + tuple(
                        (
                            copy + 1,
                            True,
                            self.policy.synthetic_weight,
                            payload + "\n" * (copy + 1),
                        )
                        for copy in range(self.policy.simulation_copies)
                    )
                    for variant_index, synthetic, weight, variant in variants:
                        for repeat in range(self.policy.repeats):
                            if len(attempts) >= self.policy.max_attempts:
                                status = BenchmarkStatus.BUDGET_EXHAUSTED
                                break
                            case_id = case.id if not synthetic else f"{case.id}-sim-{variant_index}"
                            run = lifecycle.invoke(
                                CanaryCase(CaseId(case_id), variant),
                                prepared,
                                revision,
                            )
                            verified = tuple(
                                verifier.verify(run, prepared) for verifier in verifiers
                            )
                            attempts.append(
                                CaseAttempt(case_id, repeat, synthetic, weight, run, verified)
                            )
                            try:
                                execution.reset(prepared)
                            except (OSError, RuntimeError):
                                status = BenchmarkStatus.ENVIRONMENT_ERROR
                                break
                        if status is not BenchmarkStatus.COMPLETE:
                            break
                    if status is not BenchmarkStatus.COMPLETE:
                        break
            finally:
                try:
                    execution.destroy(prepared)
                except (OSError, RuntimeError):
                    status = BenchmarkStatus.ENVIRONMENT_ERROR
        frozen_attempts = tuple(attempts)
        semantic_digest = digest_bytes(_ATTEMPTS_ADAPTER.dump_json(_semantic(frozen_attempts)))
        result_id = (
            "benchmark_result_"
            + hashlib.sha256(
                f"{self.bundle.benchmark.id}\0{self.policy.digest}\0{semantic_digest}\0{status.value}".encode()
            ).hexdigest()
        )
        result = BenchmarkResult(
            result_id,
            self.bundle.benchmark.id,
            revision.id,
            self.policy.digest,
            status,
            frozen_attempts,
            semantic_digest,
            revision.root,
        )
        write_artifact(result.manifest_path, f"{result.to_json()}\n".encode())
        return result

    def establish_baseline(self) -> Baseline:
        result = self.run()
        if result.status is not BenchmarkStatus.COMPLETE:
            raise BenchmarkError(BenchmarkErrorCode.BASELINE_INCOMPLETE, result.id)
        baseline = Baseline(
            result.benchmark_id,
            result.revision_id,
            result.policy_digest,
            result.semantic_digest,
            result.root / ".ofw" / "benchmarks" / result.benchmark_id / "baseline.json",
        )
        write_artifact(baseline.path, f"{baseline.to_json()}\n".encode())
        return baseline

    def verify_baseline(self, baseline: Baseline) -> BenchmarkResult:
        result = self.run()
        if (
            result.status is not BenchmarkStatus.COMPLETE
            or result.benchmark_id != baseline.benchmark_id
            or result.revision_id != baseline.revision_id
            or result.policy_digest != baseline.policy_digest
            or result.semantic_digest != baseline.semantic_digest
        ):
            raise BenchmarkError(BenchmarkErrorCode.BASELINE_DRIFT, result.id)
        return result

    def _revision(self) -> HarnessRevision:
        revision = self.harness.current_revision
        if revision is None:
            raise BenchmarkError(BenchmarkErrorCode.STALE_HARNESS, self.harness.name)
        if revision.id != self.bundle.revision_id:
            raise BenchmarkError(BenchmarkErrorCode.REVISION_MISMATCH, str(revision.id))
        runtime = revision.runtime
        benchmark = self.bundle.benchmark
        if (
            runtime is None
            or runtime.execution != benchmark.execution_digest
            or runtime.lifecycle != benchmark.lifecycle_digest
        ):
            raise BenchmarkError(BenchmarkErrorCode.RUNTIME_MISMATCH, benchmark.id)
        return revision


def _case_payload(case: EvalCase, root: Path) -> str:
    try:
        allowed = (root / ".ofw").resolve(strict=True)
        path = case.snapshot.path.resolve(strict=True)
        path.relative_to(allowed)
        payload = path.read_bytes()
    except (OSError, ValueError) as error:
        raise BenchmarkError(BenchmarkErrorCode.SNAPSHOT_INVALID, case.id) from error
    if digest_bytes(payload) != case.snapshot.digest:
        raise BenchmarkError(BenchmarkErrorCode.SNAPSHOT_INVALID, case.id)
    return payload.decode()


def _ledger_authorizes(case: EvalCase, bundle: ExportBundle) -> bool:
    return any(
        entry.trace_id == case.trace_id
        and entry.trace_family_id == case.family_id
        and entry.cluster_family_id == case.cluster_family_id
        and entry.partition == case.partition
        and entry.snapshot == case.snapshot
        for entry in bundle.ledger.entries
    )


def _semantic(attempts: tuple[CaseAttempt, ...]) -> tuple[CaseAttempt, ...]:
    return tuple(
        CaseAttempt(
            attempt.case_id,
            attempt.repeat,
            attempt.synthetic,
            attempt.weight,
            RunResult(
                attempt.run.case_id,
                attempt.run.status,
                attempt.run.output,
                attempt.run.error_code,
                0.0,
            ),
            attempt.verifiers,
        )
        for attempt in attempts
    )


def _digest_text(value: str) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(value.encode()).hexdigest()}")
