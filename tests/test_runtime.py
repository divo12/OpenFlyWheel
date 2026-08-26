"""Execution, lifecycle, verifier, and canary behavior."""

from __future__ import annotations

import io
import subprocess
import tarfile
import time
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from e2b.exceptions import TimeoutException

import ofw.runtime as runtime_module
from ofw import (
    CanaryCase,
    CaseId,
    CommandLoop,
    CommandVerifier,
    E2BSandbox,
    HarnessRevision,
    ModelFingerprint,
    ProcessCommand,
    ProcessLimits,
    RunErrorCode,
    RunResult,
    RunStatus,
    VerifierVerdict,
    process_repository,
)


class _FakeCommandCall:
    def __init__(
        self,
        cmd: str,
        envs: dict[str, str],
        cwd: str | None,
        timeout: float | None,
    ) -> None:
        self.cmd = cmd
        self.envs = envs
        self.cwd = cwd
        self.timeout = timeout


class _FakeFiles:
    def __init__(self, writes: dict[str, bytes | str]) -> None:
        self._writes = writes

    def write(self, path: str, data: bytes | str) -> None:
        self._writes[path] = data


class _FakeCommands:
    def __init__(self, writes: dict[str, bytes | str]) -> None:
        self._writes = writes
        self.calls: list[_FakeCommandCall] = []
        self.fail_extract = False

    def run(
        self,
        cmd: str,
        *,
        envs: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> runtime_module._E2BCommandResult:
        selected_envs = envs or {}
        self.calls.append(_FakeCommandCall(cmd, selected_envs, cwd, timeout))
        if "rm -rf /home/user/workspace" in cmd:
            exit_code = 1 if self.fail_extract else 0
            return runtime_module._E2BCommandResult("fixture", "", exit_code, None)
        if cmd.startswith("find "):
            return runtime_module._E2BCommandResult("", "", 0, None)
        payload_path = cmd.rsplit("<", 1)[1].strip().strip("'")
        raw_payload = self._writes[payload_path]
        payload = raw_payload.decode() if isinstance(raw_payload, bytes) else raw_payload
        if "timeout.py" in cmd or "verifier.py slow" in cmd:
            raise TimeoutException("fixture timeout")
        if "crash.py" in cmd or "verifier.py broken" in cmd:
            return runtime_module._E2BCommandResult("fixture crash", "", 7, "fixture")
        if "abstain.py" in cmd:
            return runtime_module._E2BCommandResult("", "not enough evidence", 2, None)
        if "agent_loop.py" in cmd:
            return runtime_module._E2BCommandResult("", payload.upper(), 0, None)
        if "verifier.py reject" in cmd:
            return runtime_module._E2BCommandResult("", "rejected", 1, None)
        if "verifier.py uppercase" in cmd:
            exit_code = 0 if payload == "SHIP" else 1
            return runtime_module._E2BCommandResult("", "uppercase output", exit_code, None)
        if "OFW_HARNESS_REVISION" in cmd:
            output = (
                selected_envs["OFW_HARNESS_REVISION"]
                + "|"
                + selected_envs["LANGFUSE_RELEASE"]
            )
            return runtime_module._E2BCommandResult("", output, 0, None)
        if cmd.startswith("printf ok"):
            return runtime_module._E2BCommandResult("", "ok", 0, None)
        return runtime_module._E2BCommandResult("", payload, 0, None)


class _FakeE2BClient:
    def __init__(self) -> None:
        self.writes: dict[str, bytes | str] = {}
        self.files = _FakeFiles(self.writes)
        self._commands = _FakeCommands(self.writes)
        self.killed = False

    @property
    def commands(self) -> _FakeCommands:
        return self._commands

    def is_running(self) -> bool:
        return True

    def kill(self) -> bool:
        self.killed = True
        return True

    @property
    def commands_calls(self) -> list[_FakeCommandCall]:
        return self._commands.calls


@pytest.fixture(autouse=True)
def fake_e2b_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_FakeE2BClient]:
    created: list[_FakeE2BClient] = []

    def create_fake_sandbox(
        *,
        template: str | None,
        timeout: int,
        api_key: str,
        environment: tuple[tuple[str, str], ...],
        allow_internet_access: bool,
    ) -> runtime_module._CreatedE2BSandbox:
        del template, timeout, api_key, allow_internet_access
        fake = _FakeE2BClient()
        created.append(fake)
        return runtime_module._CreatedE2BSandbox(fake, environment)

    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(runtime_module, "_create_e2b_sandbox", create_fake_sandbox)
    return created


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
        "import sys\nsys.stdout.write(sys.stdin.read().upper())\n",
        encoding="utf-8",
    )
    (root / "verifier.py").write_text(
        "import sys, time\n"
        "mode = sys.argv[1]\n"
        "payload = sys.stdin.read()\n"
        "if mode == 'slow': time.sleep(2)\n"
        "if mode == 'broken': raise SystemExit(7)\n"
        "if mode == 'reject': raise SystemExit(1)\n"
        "raise SystemExit(0 if payload == 'SHIP' else 1)\n",
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
    return process_repository("runtime-agent", root)


def _verifier(mode: str, name: str = "verifier") -> CommandVerifier:
    return CommandVerifier(name, ProcessCommand(("python3", "verifier.py", mode)))


def _command_loop() -> CommandLoop:
    return CommandLoop(
        command=ProcessCommand(("python3", "agent_loop.py")),
        models=(ModelFingerprint("openai", "gpt-5", "medium"),),
    )


def test_run_canary_returns_frozen_evidence(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)

    report = runtime_module.run_canary(
        revision,
        CanaryCase(CaseId("smoke"), "ship"),
        E2BSandbox(ProcessLimits(timedelta(seconds=2))),
        _command_loop(),
        (_verifier("uppercase", "uppercase"),),
    )

    assert report.passed
    assert str(report.digest).startswith("sha256:")


def test_failed_canary_is_reported_without_mutating_revision(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)

    report = runtime_module.run_canary(
        revision,
        CanaryCase(CaseId("rejected"), "ship"),
        E2BSandbox(ProcessLimits(timedelta(seconds=2))),
        _command_loop(),
        (_verifier("reject", "reject"),),
    )

    assert not report.passed
    assert _revision(root).id == revision.id


def test_e2b_reports_timeout_and_nonzero_exit(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    timeout_script = root / "timeout.py"
    timeout_script.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    crash_script = root / "crash.py"
    crash_script.write_text("raise SystemExit(7)\n", encoding="utf-8")
    timeout_environment = E2BSandbox(ProcessLimits(timedelta(milliseconds=50)))
    prepared = timeout_environment.prepare(revision, CanaryCase(CaseId("failure"), "input"))
    try:
        timed_out = CommandLoop(ProcessCommand(("python3", "timeout.py"))).invoke(
            CanaryCase(CaseId("timeout"), "input"), prepared, revision
        )
    finally:
        timeout_environment.destroy(prepared)
    crash_environment = E2BSandbox(ProcessLimits(timedelta(seconds=1)))
    prepared = crash_environment.prepare(revision, CanaryCase(CaseId("failure"), "input"))
    try:
        crashed = CommandLoop(ProcessCommand(("python3", "crash.py"))).invoke(
            CanaryCase(CaseId("crash"), "input"), prepared, revision
        )
    finally:
        crash_environment.destroy(prepared)

    assert timed_out.status is RunStatus.TIMEOUT
    assert timed_out.error_code is RunErrorCode.TIMEOUT
    assert crashed.status is RunStatus.ERROR
    assert crashed.error_code is RunErrorCode.NON_ZERO_EXIT


def test_e2b_injects_revision_attribution_without_user_code(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = E2BSandbox(ProcessLimits(timedelta(seconds=1)))
    prepared = environment.prepare(revision, CanaryCase(CaseId("attribution"), "input"))
    command = ProcessCommand(
        (
            "python3",
            "-c",
            "import os; print(os.environ['OFW_HARNESS_REVISION'] + '|' + "
            "os.environ['LANGFUSE_RELEASE'])",
        )
    )
    try:
        result = CommandLoop(command).invoke(
            CanaryCase(CaseId("attribution"), "input"),
            prepared,
            revision,
        )
    finally:
        environment.destroy(prepared)

    assert result.output == f"{revision.id}|{revision.id}"


def test_e2b_reset_restores_workspace(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = E2BSandbox(ProcessLimits(timedelta(seconds=1)))
    prepared = environment.prepare(revision, CanaryCase(CaseId("reset"), "input"))
    commands = cast(_FakeCommands, prepared.sandbox.commands)
    calls_before = len(commands.calls)

    environment.reset(prepared)

    assert len(commands.calls) == calls_before + 1
    environment.destroy(prepared)


def test_e2b_sandbox_uploads_workspace_without_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    (root / ".env").write_text(
        "E2B_API_KEY=e2b-test\nOPENAI_API_KEY=sk-test\nLANGFUSE_SECRET_KEY=lf-test\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside-secret"
    outside.write_text("must-not-upload\n", encoding="utf-8")
    (root / "outside-link").symlink_to(outside)
    revision = _revision(root)
    fake = _FakeE2BClient()
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")

    def create_fake_sandbox(
        *,
        template: str | None,
        timeout: int,
        api_key: str,
        environment: tuple[tuple[str, str], ...],
        allow_internet_access: bool,
    ) -> runtime_module._CreatedE2BSandbox:
        assert template is None
        assert timeout == 2
        assert api_key == "e2b-test"
        assert allow_internet_access
        return runtime_module._CreatedE2BSandbox(fake, environment)

    monkeypatch.setattr(runtime_module, "_create_e2b_sandbox", create_fake_sandbox)
    environment = E2BSandbox(ProcessLimits(timedelta(seconds=2)))
    prepared = environment.prepare(revision, CanaryCase(CaseId("remote"), "ship"))
    try:
        process = prepared.run(ProcessCommand(("printf", "ok")), "payload")
    finally:
        environment.destroy(prepared)

    assert not (prepared.root / ".env").exists()
    archive = fake.writes["/tmp/ofw-workspace.tar.gz"]
    assert isinstance(archive, bytes)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as uploaded:
        uploaded_names = tuple(uploaded.getnames())
    assert all(".env" not in name for name in uploaded_names)
    assert all("outside-link" not in name for name in uploaded_names)
    stdin_paths = tuple(path for path in fake.writes if path.startswith("/tmp/ofw-stdin-"))
    assert len(stdin_paths) == 1
    assert fake.writes[stdin_paths[0]] == "payload"
    command_call = fake.commands_calls[-1]
    assert command_call.cmd.startswith("printf ok < /tmp/ofw-stdin-")
    assert command_call.cwd == "/home/user/workspace"
    assert command_call.envs["OFW_HARNESS_REVISION"] == str(revision.id)
    assert command_call.envs["LANGFUSE_RELEASE"] == str(revision.id)
    assert "OPENAI_API_KEY" not in command_call.envs
    assert "LANGFUSE_SECRET_KEY" not in command_call.envs
    assert "E2B_API_KEY" not in command_call.envs
    assert process.exit_code == 0
    assert process.stdout == "ok"
    assert fake.killed


def test_e2b_hobby_timeout_is_validated() -> None:
    with pytest.raises(ValueError):
        E2BSandbox(ProcessLimits(timedelta(seconds=3601)))

    with pytest.raises(ValueError):
        E2BSandbox(
            ProcessLimits(timedelta(seconds=60)),
            forward_environment=("E2B_API_KEY",),
        )


def test_e2b_prepare_failure_kills_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    fake = _FakeE2BClient()
    fake.commands.fail_extract = True

    def create_fake_sandbox(**options: object) -> runtime_module._CreatedE2BSandbox:
        environment = cast(tuple[tuple[str, str], ...], options["environment"])
        return runtime_module._CreatedE2BSandbox(fake, environment)

    monkeypatch.setattr(runtime_module, "_create_e2b_sandbox", create_fake_sandbox)

    with pytest.raises(RuntimeError):
        E2BSandbox(ProcessLimits(timedelta(seconds=2))).prepare(
            revision,
            CanaryCase(CaseId("prepare-failure"), ""),
        )

    assert fake.killed


def test_host_execution_backends_are_not_public() -> None:
    import ofw

    assert not hasattr(ofw, "LocalProcess")
    assert not hasattr(ofw, "DockerCompose")


def test_command_verifiers_report_typed_outcomes(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = E2BSandbox(ProcessLimits(timedelta(seconds=1)))
    prepared = environment.prepare(revision, CanaryCase(CaseId("verify"), "input"))
    run = RunResult.success(CaseId("verify"), "SHIP")
    try:
        abstained = CommandVerifier(
            "command",
            ProcessCommand(("python3", "abstain.py")),
        ).verify(run, prepared)
        errored = _verifier("broken", "broken").verify(run, prepared)
    finally:
        environment.destroy(prepared)

    assert abstained.verdict is VerifierVerdict.ABSTAIN
    assert errored.verdict is VerifierVerdict.ERROR
    assert errored.retryable


def test_parallel_e2b_environments_are_distinct(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = E2BSandbox(ProcessLimits(timedelta(seconds=1)))
    case = CanaryCase(CaseId("parallel"), "input")
    first = environment.prepare(revision, case)
    second = environment.prepare(revision, case)
    try:
        assert first.sandbox is not second.sandbox
    finally:
        environment.destroy(first)
        environment.destroy(second)


def test_command_verifier_is_terminated_at_environment_timeout(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    revision = _revision(root)
    environment = E2BSandbox(ProcessLimits(timedelta(milliseconds=50)))
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
