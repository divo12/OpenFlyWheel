"""Portable Codex plugin MCP launch contract."""

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
    assert (
        "git+https://github.com/divo12/OpenFlyWheel.git@9041db3c08a89df0fe9f8f2476a303b46dd2812a"
        in server.args
    )
    runtime = (root / "src/ofw/mcp.py").read_text(encoding="utf-8")
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
