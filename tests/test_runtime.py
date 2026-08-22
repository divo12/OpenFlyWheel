"""Execution, lifecycle, verifier, and canary behavior."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from ofw import (
    CanaryCase,
    CaseId,
    CommandLoop,
    CommandVerifier,
    DockerCompose,
    FunctionName,
    Harness,
    HarnessErrorCode,
    HarnessRevision,
    HarnessValidationError,
    LocalProcess,
    ModelFingerprint,
    ModuleName,
    ProcessCommand,
    ProcessLimits,
    PythonEntrypoint,
    PythonLoop,
    PythonVerifier,
    RunErrorCode,
    RunResult,
    RunStatus,
    ServiceName,
    VerifierVerdict,
)


def _run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "runtime-agent"
    root.mkdir()
    (root / "prompt.md").write_text("Be accurate.\n", encoding="utf-8")
    (root / "agent_loop.py").write_text(
        "def run_case(value: str) -> str:\n    return value.upper()\n",
        encoding="utf-8",
    )
    (root / "verifiers.py").write_text(
        "from __future__ import annotations\n"
        "import time\n"
        "from ofw import RunResult, VerifierResult, VerifierVerdict\n"
        "def uppercase(result: RunResult) -> VerifierResult:\n"
        "    verdict = VerifierVerdict.PASS if result.output == 'SHIP' else VerifierVerdict.FAIL\n"
        "    return VerifierResult(verdict, 1.0, 'uppercase output')\n"
        "def broken(result: RunResult) -> VerifierResult:\n"
        "    del result\n"
        "    raise RuntimeError('fixture verifier failure')\n"
        "def reject(result: RunResult) -> VerifierResult:\n"
        "    del result\n"
        "    return VerifierResult(VerifierVerdict.FAIL, 0.0, 'fixture rejection')\n"
        "def slow(result: RunResult) -> VerifierResult:\n"
        "    del result\n"
        "    time.sleep(2)\n"
        "    return VerifierResult(VerifierVerdict.PASS, 1.0, 'late')\n",
        encoding="utf-8",
    )
    (root / "compose.yaml").write_text(
        "services:\n  agent:\n    image: fixture-agent:latest\n",
        encoding="utf-8",
    )
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "fixture@example.test")
    _run_git(root, "config", "user.name", "FixtureCo")
    _run_git(root, "add", ".")
    _run_git(root, "commit", "-qm", "fixture baseline")
    return root


def _revision(root: Path) -> HarnessRevision:
    harness = Harness("runtime-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    return harness.process()


def _verifier(function: str, name: str = "verifier") -> PythonVerifier:
    return PythonVerifier(
        name,
        PythonEntrypoint(ModuleName("verifiers"), FunctionName(function)),
    )


def _python_loop() -> PythonLoop:
    return PythonLoop(
        entrypoint=PythonEntrypoint(ModuleName("agent_loop"), FunctionName("run_case")),
        models=(ModelFingerprint("openai", "gpt-5", "medium"),),
    )


def _runtime_harness(root: Path) -> Harness:
    harness = Harness("runtime-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=2))))
    harness.connect_lifecycle(_python_loop())
    harness.connect_verifiers(_verifier("uppercase", "uppercase"))
    return harness


def test_process_runs_local_python_canary_and_records_frozen_evidence(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    harness = Harness("runtime-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=2))))
    harness.connect_lifecycle(_python_loop())
    harness.connect_verifiers(_verifier("uppercase", "uppercase"))

    revision = harness.process(canary=CanaryCase(CaseId("smoke"), "ship"))

    assert revision.runtime is not None
    assert revision.canary_digest is not None
    assert revision.canary_path.is_file()
    assert "pass" in revision.canary_path.read_text(encoding="utf-8")


def test_canary_evidence_does_not_change_runtime_revision_identity(tmp_path: Path) -> None:
    root = _repository(tmp_path)

    without_canary = _runtime_harness(root).process()
    with_canary = _runtime_harness(root).process(canary=CanaryCase(CaseId("identity"), "ship"))

    assert with_canary.id == without_canary.id
    assert with_canary.canary_digest is not None


def test_partial_runtime_configuration_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    harness = Harness("runtime-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=1))))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.RUNTIME_INCOMPLETE


def test_duplicate_verifier_name_is_rejected(tmp_path: Path) -> None:
    harness = Harness("runtime-agent", root=_repository(tmp_path))

    with pytest.raises(HarnessValidationError) as raised:
        harness.connect_verifiers(
            _verifier("uppercase", "duplicate"),
            _verifier("reject", "duplicate"),
        )

    assert raised.value.code is HarnessErrorCode.DUPLICATE_VERIFIER


def test_failed_canary_blocks_revision_creation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    harness = Harness("runtime-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=2))))
    harness.connect_lifecycle(_python_loop())
    harness.connect_verifiers(_verifier("reject", "reject"))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process(canary=CanaryCase(CaseId("rejected"), "ship"))

    assert raised.value.code is HarnessErrorCode.CANARY_FAILED


def test_local_process_reports_timeout_and_nonzero_exit(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    timeout_script = root / "timeout.py"
    timeout_script.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    crash_script = root / "crash.py"
    crash_script.write_text("raise SystemExit(7)\n", encoding="utf-8")
    timeout_environment = LocalProcess(ProcessLimits(timedelta(milliseconds=50)))
    prepared = timeout_environment.prepare(revision, CanaryCase(CaseId("failure"), "input"))
    try:
        timed_out = CommandLoop(ProcessCommand((sys.executable, "timeout.py"))).invoke(
            CanaryCase(CaseId("timeout"), "input"), prepared, revision
        )
    finally:
        timeout_environment.destroy(prepared)
    crash_environment = LocalProcess(ProcessLimits(timedelta(seconds=1)))
    prepared = crash_environment.prepare(revision, CanaryCase(CaseId("failure"), "input"))
    try:
        crashed = CommandLoop(ProcessCommand((sys.executable, "crash.py"))).invoke(
            CanaryCase(CaseId("crash"), "input"), prepared, revision
        )
    finally:
        crash_environment.destroy(prepared)

    assert timed_out.status is RunStatus.TIMEOUT
    assert timed_out.error_code is RunErrorCode.TIMEOUT
    assert crashed.status is RunStatus.ERROR
    assert crashed.error_code is RunErrorCode.NON_ZERO_EXIT


def test_local_process_reset_restores_workspace(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = LocalProcess(ProcessLimits(timedelta(seconds=1)))
    prepared = environment.prepare(revision, CanaryCase(CaseId("reset"), "input"))
    copied_prompt = prepared.root / "prompt.md"
    copied_prompt.write_text("mutated\n", encoding="utf-8")

    environment.reset(prepared)

    assert copied_prompt.read_text(encoding="utf-8") == "Be accurate.\n"
    environment.destroy(prepared)
    assert not prepared.root.exists()


def test_python_and_command_verifiers_report_typed_outcomes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = LocalProcess(ProcessLimits(timedelta(seconds=1)))
    prepared = environment.prepare(revision, CanaryCase(CaseId("verify"), "input"))
    abstain_script = prepared.root / "abstain.py"
    abstain_script.write_text(
        "import sys\nprint('not enough evidence')\nraise SystemExit(2)\n",
        encoding="utf-8",
    )
    run = RunResult.success(CaseId("verify"), "SHIP")
    try:
        abstained = CommandVerifier(
            "command",
            ProcessCommand((sys.executable, "abstain.py")),
        ).verify(run, prepared)
        errored = _verifier("broken", "broken").verify(run, prepared)
    finally:
        environment.destroy(prepared)

    assert abstained.verdict is VerifierVerdict.ABSTAIN
    assert errored.verdict is VerifierVerdict.ERROR
    assert errored.retryable


def test_docker_compose_adapter_uses_disposable_native_lifecycle(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = DockerCompose(
        compose_file=Path("compose.yaml"),
        service=ServiceName("agent"),
        executable=Path("/usr/bin/true"),
        limits=ProcessLimits(timedelta(seconds=1)),
    )
    prepared = environment.prepare(revision, CanaryCase(CaseId("docker"), "input"))

    assert environment.health(prepared).passed
    (prepared.root / "prompt.md").write_text("mutated\n", encoding="utf-8")
    environment.reset(prepared)
    assert (prepared.root / "prompt.md").read_text(encoding="utf-8") == "Be accurate.\n"
    environment.destroy(prepared)
    assert not prepared.root.exists()


def test_parallel_docker_environments_have_distinct_projects(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = DockerCompose(
        compose_file=Path("compose.yaml"),
        service=ServiceName("agent"),
        executable=Path("/usr/bin/true"),
        limits=ProcessLimits(timedelta(seconds=1)),
    )
    case = CanaryCase(CaseId("parallel"), "input")
    first = environment.prepare(revision, case)
    second = environment.prepare(revision, case)
    try:
        assert first.command_prefix != second.command_prefix
    finally:
        environment.destroy(first)
        environment.destroy(second)


def test_runtime_fingerprint_changes_without_changing_assets(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    first = Harness("runtime-agent", root=root)
    first.connect_prompt(Path("prompt.md"))
    first.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=1))))
    first.connect_lifecycle(_python_loop())
    first.connect_verifiers(_verifier("uppercase", "uppercase"))
    second = Harness("runtime-agent", root=root)
    second.connect_prompt(Path("prompt.md"))
    second.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=2))))
    second.connect_lifecycle(_python_loop())
    second.connect_verifiers(_verifier("uppercase", "uppercase"))

    first_revision = first.process()
    second_revision = second.process()

    assert first_revision.id != second_revision.id
    assert first_revision.components == second_revision.components


def test_missing_python_entrypoint_is_rejected_during_process(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    harness = Harness("runtime-agent", root=root)
    harness.connect_prompt(Path("prompt.md"))
    harness.connect_execute(LocalProcess(ProcessLimits(timedelta(seconds=1))))
    harness.connect_lifecycle(
        PythonLoop(PythonEntrypoint(ModuleName("missing_module"), FunctionName("run")))
    )
    harness.connect_verifiers(_verifier("uppercase"))

    with pytest.raises(HarnessValidationError) as raised:
        harness.process()

    assert raised.value.code is HarnessErrorCode.RUNTIME_INVALID


def test_python_verifier_is_terminated_at_environment_timeout(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = LocalProcess(ProcessLimits(timedelta(milliseconds=50)))
    prepared = environment.prepare(revision, CanaryCase(CaseId("slow"), "ship"))
    started = time.monotonic()
    try:
        result = _verifier("slow", "slow").verify(
            RunResult.success(CaseId("slow"), "SHIP"),
            prepared,
        )
    finally:
        environment.destroy(prepared)

    assert result.verdict is VerifierVerdict.ERROR
    assert result.retryable
    assert time.monotonic() - started < 1
