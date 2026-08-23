"""Disposable execution, lifecycle, verifier, and canary adapters."""

from __future__ import annotations

import ast
import hashlib
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from ofw.contracts import HarnessRevision, RuntimeConfiguration, Sha256Digest

_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
_MODULE_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.]*")
_FUNCTION_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


class RunStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


class RunErrorCode(StrEnum):
    NON_ZERO_EXIT = "non_zero_exit"
    TIMEOUT = "timeout"


class VerifierVerdict(StrEnum):
    PASS = "pass"  # nosec B105
    FAIL = "fail"
    ABSTAIN = "abstain"
    ERROR = "error"


class VerifierExitCode(IntEnum):
    PASS = 0
    FAIL = 1
    ABSTAIN = 2


@dataclass(frozen=True, slots=True)
class CaseId:
    value: str

    def __post_init__(self) -> None:
        if _NAME_PATTERN.fullmatch(self.value) is None:
            raise ValueError("invalid case id")


@dataclass(frozen=True, slots=True)
class CanaryCase:
    id: CaseId
    payload: str


@dataclass(frozen=True, slots=True)
class ModuleName:
    value: str

    def __post_init__(self) -> None:
        if _MODULE_PATTERN.fullmatch(self.value) is None:
            raise ValueError("invalid module name")


@dataclass(frozen=True, slots=True)
class FunctionName:
    value: str

    def __post_init__(self) -> None:
        if _FUNCTION_PATTERN.fullmatch(self.value) is None:
            raise ValueError("invalid function name")


@dataclass(frozen=True, slots=True)
class PythonEntrypoint:
    module: ModuleName
    function: FunctionName


@dataclass(frozen=True, slots=True)
class ServiceName:
    value: str

    def __post_init__(self) -> None:
        if _NAME_PATTERN.fullmatch(self.value) is None:
            raise ValueError("invalid service name")


@dataclass(frozen=True, slots=True)
class ModelFingerprint:
    provider: str
    model: str
    reasoning: str

    def __post_init__(self) -> None:
        if not self.provider or not self.model or not self.reasoning:
            raise ValueError("model fingerprint fields are required")


@dataclass(frozen=True, slots=True)
class ProcessCommand:
    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.arguments or any(not argument for argument in self.arguments):
            raise ValueError("command arguments are required")


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    timeout: timedelta

    def __post_init__(self) -> None:
        if self.timeout <= timedelta(0):
            raise ValueError("timeout must be positive")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int | None
    stdout: str
    timed_out: bool
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class RunResult:
    case_id: CaseId
    status: RunStatus
    output: str | None
    error_code: RunErrorCode | None
    duration_seconds: float

    @classmethod
    def success(cls, case_id: CaseId, output: str) -> RunResult:
        return cls(case_id, RunStatus.SUCCESS, output, None, 0.0)


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    value: float


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    value: str


@dataclass(frozen=True, slots=True)
class VerifierResult:
    verdict: VerifierVerdict
    score: float | None
    feedback: str
    metrics: tuple[Metric, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class CheckReport:
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class EnvironmentFingerprint:
    digest: Sha256Digest


@dataclass(frozen=True, slots=True)
class PreparedEnvironment:
    root: Path
    source_root: Path
    temporary: tempfile.TemporaryDirectory[str]
    limits: ProcessLimits
    command_prefix: tuple[str, ...] = ()

    def run(self, command: ProcessCommand, payload: str) -> ProcessResult:
        started = time.monotonic()
        try:
            # The revision freezes argv; every call uses shell=False.
            completed = subprocess.run(  # nosec B603
                (*self.command_prefix, *command.arguments),
                cwd=self.root,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.limits.timeout.total_seconds(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ProcessResult(None, "", True, time.monotonic() - started)
        return ProcessResult(
            completed.returncode,
            completed.stdout.rstrip("\n"),
            False,
            time.monotonic() - started,
        )


@dataclass(frozen=True, slots=True)
class LocalProcess:
    limits: ProcessLimits

    def fingerprint(self, root: Path) -> Sha256Digest:
        del root
        return _digest_text(
            f"local\0{self.limits.timeout.total_seconds()}\0{sys.version}\0{sys.platform}"
        )

    def prepare(self, revision: HarnessRevision, case: CanaryCase) -> PreparedEnvironment:
        del case
        return _prepare_workspace(revision.root, self.limits)

    def health(self, prepared: PreparedEnvironment) -> CheckReport:
        return CheckReport(prepared.root.is_dir(), "local workspace")

    def snapshot(self, prepared: PreparedEnvironment) -> EnvironmentFingerprint:
        return EnvironmentFingerprint(_digest_tree(prepared.root))

    def reset(self, prepared: PreparedEnvironment) -> None:
        _restore_workspace(prepared)

    def destroy(self, prepared: PreparedEnvironment) -> None:
        prepared.temporary.cleanup()


@dataclass(frozen=True, slots=True)
class DockerCompose:
    compose_file: Path
    service: ServiceName
    executable: Path
    limits: ProcessLimits

    def fingerprint(self, root: Path) -> Sha256Digest:
        compose = root / self.compose_file
        return _digest_text(
            f"docker\0{self.service.value}\0{self.executable}\0"
            f"{self.limits.timeout.total_seconds()}\0{_digest_file(compose)}"
        )

    def prepare(self, revision: HarnessRevision, case: CanaryCase) -> PreparedEnvironment:
        del case
        workspace = _prepare_workspace(revision.root, self.limits)
        prepared = PreparedEnvironment(
            workspace.root,
            workspace.source_root,
            workspace.temporary,
            workspace.limits,
            (
                *_compose_prefix(self, workspace),
                "exec",
                "-T",
                self.service.value,
            ),
        )
        if not (prepared.root / self.compose_file).is_file():
            self.destroy(prepared)
            raise RuntimeError("compose file is missing")
        started = _compose_control(self, prepared, ("up", "-d", self.service.value))
        if started.exit_code != 0:
            self.destroy(prepared)
            raise RuntimeError("docker compose failed to start")
        return prepared

    def health(self, prepared: PreparedEnvironment) -> CheckReport:
        result = _compose_control(self, prepared, ("ps", "--status", "running"))
        return CheckReport(result.exit_code == 0, "docker compose")

    def snapshot(self, prepared: PreparedEnvironment) -> EnvironmentFingerprint:
        return EnvironmentFingerprint(_digest_tree(prepared.root))

    def reset(self, prepared: PreparedEnvironment) -> None:
        stopped = _compose_control(self, prepared, ("down", "--volumes", "--remove-orphans"))
        if stopped.exit_code != 0:
            raise RuntimeError("docker compose reset failed")
        _restore_workspace(prepared)
        started = _compose_control(self, prepared, ("up", "-d", self.service.value))
        if started.exit_code != 0:
            raise RuntimeError("docker compose reset failed")

    def destroy(self, prepared: PreparedEnvironment) -> None:
        _compose_control(self, prepared, ("down", "--volumes", "--remove-orphans"))
        prepared.temporary.cleanup()


ExecutionEnvironment = LocalProcess | DockerCompose


@dataclass(frozen=True, slots=True)
class CommandLoop:
    command: ProcessCommand
    models: tuple[ModelFingerprint, ...] = ()

    def fingerprint(self, root: Path) -> Sha256Digest:
        return _command_fingerprint("command-loop", self.command, self.models, root)

    def invoke(
        self,
        case: CanaryCase,
        prepared: PreparedEnvironment,
        revision: HarnessRevision,
    ) -> RunResult:
        del revision
        return _run_result(case.id, prepared.run(self.command, case.payload))


@dataclass(frozen=True, slots=True)
class PythonLoop:
    entrypoint: PythonEntrypoint
    models: tuple[ModelFingerprint, ...] = ()

    def fingerprint(self, root: Path) -> Sha256Digest:
        module_path = resolve_python_source(root, self.entrypoint)
        return _digest_text(
            f"python-loop\0{self.entrypoint.module.value}\0"
            f"{self.entrypoint.function.value}\0{_digest_file(module_path)}\0"
            f"{_models_text(self.models)}"
        )

    def invoke(
        self,
        case: CanaryCase,
        prepared: PreparedEnvironment,
        revision: HarnessRevision,
    ) -> RunResult:
        del revision
        command = ProcessCommand(
            (
                sys.executable,
                "-m",
                "ofw._runner",
                self.entrypoint.module.value,
                self.entrypoint.function.value,
            )
        )
        return _run_result(case.id, prepared.run(command, case.payload))


LifecycleAdapter = CommandLoop | PythonLoop


@dataclass(frozen=True, slots=True)
class PythonVerifier:
    name: str
    entrypoint: PythonEntrypoint

    def __post_init__(self) -> None:
        if _NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError("invalid verifier name")

    def fingerprint(self, root: Path) -> Sha256Digest:
        source = resolve_python_source(root, self.entrypoint)
        return _digest_text(
            f"python-verifier\0{self.name}\0{self.entrypoint.module.value}\0"
            f"{self.entrypoint.function.value}\0{_digest_file(source)}"
        )

    def verify(
        self,
        result: RunResult,
        prepared: PreparedEnvironment,
    ) -> VerifierResult:
        command = ProcessCommand(
            (
                sys.executable,
                "-m",
                "ofw._verifier_runner",
                self.entrypoint.module.value,
                self.entrypoint.function.value,
            )
        )
        process = prepared.run(command, _RUN_ADAPTER.dump_json(result).decode())
        if process.timed_out:
            return VerifierResult(
                VerifierVerdict.ERROR,
                None,
                "python verifier timed out",
                retryable=True,
            )
        if process.exit_code != 0:
            return VerifierResult(
                VerifierVerdict.ERROR,
                None,
                "python verifier failed",
                retryable=True,
            )
        try:
            return _VERIFIER_ADAPTER.validate_json(process.stdout)
        except ValidationError:
            return VerifierResult(VerifierVerdict.ERROR, None, "invalid verifier result")


@dataclass(frozen=True, slots=True)
class CommandVerifier:
    name: str
    command: ProcessCommand

    def __post_init__(self) -> None:
        if _NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError("invalid verifier name")

    def fingerprint(self, root: Path) -> Sha256Digest:
        return _command_fingerprint("command-verifier", self.command, (), root)

    def verify(
        self,
        result: RunResult,
        prepared: PreparedEnvironment,
    ) -> VerifierResult:
        process = prepared.run(self.command, result.output or "")
        if process.timed_out:
            return VerifierResult(
                VerifierVerdict.ERROR,
                None,
                "command verifier timed out",
                retryable=True,
            )
        if process.exit_code == VerifierExitCode.PASS:
            return VerifierResult(VerifierVerdict.PASS, 1.0, process.stdout)
        if process.exit_code == VerifierExitCode.FAIL:
            return VerifierResult(VerifierVerdict.FAIL, 0.0, process.stdout)
        if process.exit_code == VerifierExitCode.ABSTAIN:
            return VerifierResult(VerifierVerdict.ABSTAIN, None, process.stdout)
        return VerifierResult(VerifierVerdict.ERROR, None, "command verifier failed")


VerifierAdapter = PythonVerifier | CommandVerifier

_RUN_ADAPTER: TypeAdapter[RunResult] = TypeAdapter(RunResult)
_VERIFIER_ADAPTER: TypeAdapter[VerifierResult] = TypeAdapter(VerifierResult)


@dataclass(frozen=True, slots=True)
class CanaryReport:
    case_id: CaseId
    health: CheckReport
    run: RunResult
    verifiers: tuple[VerifierResult, ...]

    @property
    def digest(self) -> Sha256Digest:
        return _digest_bytes(_CANARY_ADAPTER.dump_json(self))

    @property
    def passed(self) -> bool:
        return (
            self.health.passed
            and self.run.status is RunStatus.SUCCESS
            and bool(self.verifiers)
            and all(result.verdict is VerifierVerdict.PASS for result in self.verifiers)
        )

    def to_json(self) -> str:
        return _CANARY_ADAPTER.dump_json(self).decode()


_CANARY_ADAPTER: TypeAdapter[CanaryReport] = TypeAdapter(CanaryReport)


def runtime_configuration(
    root: Path,
    execution: ExecutionEnvironment,
    lifecycle: LifecycleAdapter,
    verifiers: tuple[VerifierAdapter, ...],
) -> RuntimeConfiguration:
    return RuntimeConfiguration(
        execution.fingerprint(root),
        lifecycle.fingerprint(root),
        tuple(verifier.fingerprint(root) for verifier in verifiers),
    )


def run_canary(
    revision: HarnessRevision,
    case: CanaryCase,
    execution: ExecutionEnvironment,
    lifecycle: LifecycleAdapter,
    verifiers: tuple[VerifierAdapter, ...],
) -> CanaryReport:
    prepared = execution.prepare(revision, case)
    try:
        health = execution.health(prepared)
        run = lifecycle.invoke(case, prepared, revision)
        results = tuple(verifier.verify(run, prepared) for verifier in verifiers)
        return CanaryReport(case.id, health, run, results)
    finally:
        execution.destroy(prepared)


def _prepare_workspace(
    source_root: Path,
    limits: ProcessLimits,
    command_prefix: tuple[str, ...] = (),
) -> PreparedEnvironment:
    temporary = tempfile.TemporaryDirectory(prefix="ofw-runtime-")
    root = Path(temporary.name) / "workspace"
    _copy_workspace(source_root, root)
    return PreparedEnvironment(root, source_root, temporary, limits, command_prefix)


def _copy_workspace(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=_ignored_artifacts,
    )


def _ignored_artifacts(directory: str, names: list[str]) -> set[str]:
    del directory
    ignored = (".git", ".ofw", ".venv", "__pycache__", "build", "dist")
    return {name for name in names if name in ignored}


def _restore_workspace(prepared: PreparedEnvironment) -> None:
    shutil.rmtree(prepared.root)
    _copy_workspace(prepared.source_root, prepared.root)


def _compose_control(
    adapter: DockerCompose,
    prepared: PreparedEnvironment,
    arguments: tuple[str, ...],
) -> ProcessResult:
    command = ProcessCommand(
        (
            *_compose_prefix(adapter, prepared),
            *arguments,
        )
    )
    local = PreparedEnvironment(
        prepared.root,
        prepared.source_root,
        prepared.temporary,
        prepared.limits,
    )
    return local.run(command, "")


def _compose_prefix(
    adapter: DockerCompose,
    prepared: PreparedEnvironment,
) -> tuple[str, ...]:
    project = hashlib.sha256(prepared.temporary.name.encode()).hexdigest()[:16]
    return (
        str(adapter.executable),
        "compose",
        "-f",
        adapter.compose_file.as_posix(),
        "-p",
        f"ofw-{project}",
    )


def _run_result(case_id: CaseId, process: ProcessResult) -> RunResult:
    if process.timed_out:
        return RunResult(
            case_id, RunStatus.TIMEOUT, None, RunErrorCode.TIMEOUT, process.duration_seconds
        )
    if process.exit_code != 0:
        return RunResult(
            case_id,
            RunStatus.ERROR,
            None,
            RunErrorCode.NON_ZERO_EXIT,
            process.duration_seconds,
        )
    return RunResult(case_id, RunStatus.SUCCESS, process.stdout, None, process.duration_seconds)


def _command_fingerprint(
    kind: str,
    command: ProcessCommand,
    models: tuple[ModelFingerprint, ...],
    root: Path,
) -> Sha256Digest:
    sources = tuple(
        _digest_file(root / argument)
        for argument in command.arguments
        if (root / argument).is_file()
    )
    return _digest_text("\0".join((kind, *command.arguments, *sources, _models_text(models))))


def resolve_python_source(root: Path, entrypoint: PythonEntrypoint) -> Path:
    path = root / Path(*entrypoint.module.value.split(".")).with_suffix(".py")
    if not path.is_file():
        raise ValueError("python module is missing")
    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as error:
        raise ValueError("python module is invalid") from error
    if not any(
        isinstance(node, ast.FunctionDef) and node.name == entrypoint.function.value
        for node in module.body
    ):
        raise ValueError("top-level python function is missing")
    return path


def _models_text(models: tuple[ModelFingerprint, ...]) -> str:
    return "\0".join(f"{model.provider}:{model.model}:{model.reasoning}" for model in models)


def _digest_tree(root: Path) -> Sha256Digest:
    payload = "\0".join(
        f"{path.relative_to(root).as_posix()}:{_digest_file(path)}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    return _digest_text(payload)


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest_text(value: str) -> Sha256Digest:
    return _digest_bytes(value.encode())


def _digest_bytes(value: bytes) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(value).hexdigest()}")
