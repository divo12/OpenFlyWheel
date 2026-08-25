"""Disposable execution, lifecycle, verifier, and canary adapters."""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Protocol, cast

from pydantic import TypeAdapter

from ofw.contracts import HarnessRevision, RuntimeConfiguration, Sha256Digest

_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
_ENVIRONMENT_PATTERN = re.compile(r"[A-Z_][A-Z0-9_]*")
_E2B_REMOTE_ROOT = "/home/user/workspace"
_E2B_WORKSPACE_ARCHIVE = "/tmp/ofw-workspace.tar.gz"  # nosec B108 - per-run sandbox
_E2B_HOBBY_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class _E2BCommandResult:
    stderr: str
    stdout: str
    exit_code: int
    error: str | None


class _E2BFiles(Protocol):
    def write(self, path: str, data: bytes | str) -> object: ...


class _E2BCommands(Protocol):
    def run(
        self,
        cmd: str,
        *,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> _E2BCommandResult: ...


class _E2BClient(Protocol):
    @property
    def files(self) -> _E2BFiles: ...

    @property
    def commands(self) -> _E2BCommands: ...

    def is_running(self) -> bool: ...

    def kill(self) -> bool: ...


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
    environment: tuple[tuple[str, str], ...]
    sandbox: _E2BClient
    remote_root: str

    def run(self, command: ProcessCommand, payload: str) -> ProcessResult:
        started = time.monotonic()
        return _run_e2b_command(self, command, payload, started)


@dataclass(frozen=True, slots=True)
class _StagedWorkspace:
    root: Path
    source_root: Path
    temporary: tempfile.TemporaryDirectory[str]
    limits: ProcessLimits


@dataclass(frozen=True, slots=True)
class E2BSandbox:
    limits: ProcessLimits
    template: str | None = None
    allow_internet_access: bool = True
    forward_environment: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        seconds = math.ceil(self.limits.timeout.total_seconds())
        if seconds > _E2B_HOBBY_TIMEOUT_SECONDS:
            raise ValueError("E2B Hobby sandboxes support at most 3600 seconds")
        reserved = {"E2B_API_KEY", "OFW_HARNESS_REVISION", "LANGFUSE_RELEASE"}
        if (
            len(set(self.forward_environment)) != len(self.forward_environment)
            or any(
                _ENVIRONMENT_PATTERN.fullmatch(name) is None
                for name in self.forward_environment
            )
            or any(name in reserved for name in self.forward_environment)
        ):
            raise ValueError("invalid forwarded environment")

    def fingerprint(self, root: Path) -> Sha256Digest:
        del root
        return _digest_text(
            f"e2b\0{self.template or ''}\0{self.allow_internet_access}\0"
            f"{self.limits.timeout.total_seconds()}\0{','.join(self.forward_environment)}"
        )

    def prepare(self, revision: HarnessRevision, case: CanaryCase) -> PreparedEnvironment:
        del case
        workspace = _prepare_workspace(revision.root, self.limits)
        control_environment = _control_environment()
        runtime_environment = _runtime_environment(revision.root)
        timeout = max(1, math.ceil(self.limits.timeout.total_seconds()))
        sandbox = _create_e2b_sandbox(
            template=self.template,
            timeout=timeout,
            api_key=_required_env(control_environment, "E2B_API_KEY"),
            environment=_revision_environment(revision)
            + _forwarded_environment(runtime_environment, self.forward_environment),
            allow_internet_access=self.allow_internet_access,
        )
        prepared = PreparedEnvironment(
            workspace.root,
            workspace.source_root,
            workspace.temporary,
            workspace.limits,
            environment=sandbox.envs,
            sandbox=sandbox.client,
            remote_root=_E2B_REMOTE_ROOT,
        )
        try:
            archive = _workspace_archive(workspace.root)
            sandbox.client.files.write(_E2B_WORKSPACE_ARCHIVE, archive)
            _extract_e2b_workspace(prepared)
        except Exception:
            try:
                sandbox.client.kill()
            finally:
                workspace.temporary.cleanup()
            raise
        return prepared

    def health(self, prepared: PreparedEnvironment) -> CheckReport:
        return CheckReport(prepared.sandbox.is_running(), "e2b sandbox")

    def snapshot(self, prepared: PreparedEnvironment) -> EnvironmentFingerprint:
        result = prepared.sandbox.commands.run(
            "find . -type f -print0 | sort -z | xargs -0 sha256sum",
            cwd=prepared.remote_root,
            timeout=prepared.limits.timeout.total_seconds(),
        )
        return EnvironmentFingerprint(_digest_text(result.stdout))

    def reset(self, prepared: PreparedEnvironment) -> None:
        _extract_e2b_workspace(prepared)

    def destroy(self, prepared: PreparedEnvironment) -> None:
        try:
            prepared.sandbox.kill()
        finally:
            prepared.temporary.cleanup()


@dataclass(frozen=True, slots=True)
class _CreatedE2BSandbox:
    client: _E2BClient
    envs: tuple[tuple[str, str], ...]


ExecutionEnvironment = E2BSandbox


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


LifecycleAdapter = CommandLoop


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
        return VerifierResult(
            VerifierVerdict.ERROR,
            None,
            "command verifier failed",
            retryable=True,
        )


VerifierAdapter = CommandVerifier


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
) -> _StagedWorkspace:
    temporary = tempfile.TemporaryDirectory(prefix="ofw-runtime-")
    root = Path(temporary.name) / "workspace"
    _copy_workspace(source_root, root)
    return _StagedWorkspace(root, source_root, temporary, limits)


def _copy_workspace(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=_ignored_artifacts,
    )


def _ignored_artifacts(directory: str, names: list[str]) -> set[str]:
    directory_path = Path(directory)
    fixed = {".git", ".ofw", ".venv", "__pycache__", "build", "dist"}
    return {
        name
        for name in names
        if name in fixed
        or (name == ".env" or (name.startswith(".env.") and name != ".env.example"))
        or (directory_path / name).is_symlink()
    }


def _revision_environment(revision: HarnessRevision) -> tuple[tuple[str, str], ...]:
    revision_id = str(revision.id)
    return (
        ("OFW_HARNESS_REVISION", revision_id),
        ("LANGFUSE_RELEASE", revision_id),
    )


def _create_e2b_sandbox(
    *,
    template: str | None,
    timeout: int,
    api_key: str,
    environment: tuple[tuple[str, str], ...],
    allow_internet_access: bool,
) -> _CreatedE2BSandbox:
    from e2b import Sandbox

    revision_id = dict(environment)["OFW_HARNESS_REVISION"]
    client = cast(
        _E2BClient,
        Sandbox.create(
            template=template,
            timeout=timeout,
            api_key=api_key,
            envs=dict(environment),
            metadata={"ofw.harness.revision": revision_id},
            secure=True,
            allow_internet_access=allow_internet_access,
        ),
    )
    return _CreatedE2BSandbox(client, environment)


def _run_e2b_command(
    prepared: PreparedEnvironment,
    command: ProcessCommand,
    payload: str,
    started: float,
) -> ProcessResult:
    from e2b import CommandExitException, TimeoutException

    stdin_path = f"/tmp/ofw-stdin-{uuid.uuid4().hex}"  # nosec B108 - per-run sandbox
    prepared.sandbox.files.write(stdin_path, payload)
    timeout = prepared.limits.timeout.total_seconds()
    try:
        result = prepared.sandbox.commands.run(
            f"{shlex.join(command.arguments)} < {shlex.quote(stdin_path)}",
            cwd=prepared.remote_root,
            envs=dict(prepared.environment),
            timeout=timeout,
        )
    except TimeoutException:
        return ProcessResult(None, "", True, time.monotonic() - started)
    except CommandExitException as error:
        result = _E2BCommandResult(error.stderr, error.stdout, error.exit_code, error.error)
    return ProcessResult(
        result.exit_code,
        result.stdout.rstrip("\n"),
        False,
        time.monotonic() - started,
    )


def _extract_e2b_workspace(prepared: PreparedEnvironment) -> None:
    result = prepared.sandbox.commands.run(
        " && ".join(
            (
                f"rm -rf {shlex.quote(prepared.remote_root)}",
                f"mkdir -p {shlex.quote(prepared.remote_root)}",
                f"tar -xzf {shlex.quote(_E2B_WORKSPACE_ARCHIVE)} -C "
                f"{shlex.quote(prepared.remote_root)}",
            )
        ),
        timeout=prepared.limits.timeout.total_seconds(),
    )
    if result.exit_code != 0:
        raise RuntimeError("e2b workspace restore failed")


def _workspace_archive(root: Path) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        archive.add(root, arcname=".")
    return stream.getvalue()


def _runtime_environment(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (Path.cwd() / ".env", root / ".env"):
        if path.is_file():
            values.update(_dotenv_values(path))
    values.update(os.environ)
    return values


def _control_environment() -> dict[str, str]:
    values = _dotenv_values(Path.cwd() / ".env") if (Path.cwd() / ".env").is_file() else {}
    values.update(os.environ)
    return values


def _dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*", line)
        if match is None:
            continue
        value: str = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def _required_env(values: dict[str, str], name: str) -> str:
    value = values.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _forwarded_environment(
    values: dict[str, str],
    names: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple((name, values[name]) for name in names if values.get(name))


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


def _models_text(models: tuple[ModelFingerprint, ...]) -> str:
    return "\0".join(f"{model.provider}:{model.model}:{model.reasoning}" for model in models)


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest_text(value: str) -> Sha256Digest:
    return _digest_bytes(value.encode())


def _digest_bytes(value: bytes) -> Sha256Digest:
    return Sha256Digest(f"sha256:{hashlib.sha256(value).hexdigest()}")
