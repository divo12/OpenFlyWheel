"""Portable Codex plugin MCP launch contract."""

import re
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _McpServer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command: Literal["uvx"]
    args: tuple[str, ...] = Field(min_length=5)
    env_vars: tuple[str, ...] = Field(min_length=1)
    startup_timeout_sec: int = Field(ge=1)
    tool_timeout_sec: int = Field(ge=1)


class _McpManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mcpServers: dict[str, _McpServer]


def test_openflywheel_mcp_uses_pinned_portable_runtime() -> None:
    root = Path(__file__).parents[1]
    path = root / "plugins/openflywheel/.mcp.json"
    manifest = _McpManifest.model_validate_json(path.read_text(encoding="utf-8"))
    server = manifest.mcpServers["openflywheel"]
    source = server.args[1]
    match = re.fullmatch(
        r"git\+https://github\.com/divo12/OpenFlyWheel\.git@([0-9a-f]{40})",
        source,
    )

    assert match is not None
    runtime = subprocess.run(
        ("git", "show", f"{match.group(1)}:src/ofw/mcp.py"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "def record_hypothesis(" in runtime
    assert "openflywheel-mcp" in server.args
    assert "PLUGIN_ROOT" not in path.read_text(encoding="utf-8")
    assert "OPENFLYWHEEL_ROOT" not in path.read_text(encoding="utf-8")
    assert "LANGFUSE_SECRET_KEY" in server.env_vars


def test_plugin_and_runtime_package_versions_match() -> None:
    root = Path(__file__).parents[1]
    plugin_version_line = next(
        line
        for line in (root / "plugins/openflywheel/.codex-plugin/plugin.json")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip().startswith('"version":')
    )
    package_version_line = next(
        line
        for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines()
        if line.startswith("version = ")
    )

    assert plugin_version_line.split('"')[3] == package_version_line.split('"')[1]
