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
    path = Path(__file__).parents[1] / "plugins/openflywheel/.mcp.json"
    manifest = _McpManifest.model_validate_json(path.read_text(encoding="utf-8"))
    server = manifest.mcpServers["openflywheel"]

    assert (
        "git+https://github.com/divo12/OpenFlyWheel.git@7076900824da68b3ba62690985f893c273d5748a"
        in server.args
    )
    assert "openflywheel-mcp" in server.args
    assert "PLUGIN_ROOT" not in path.read_text(encoding="utf-8")
    assert "OPENFLYWHEEL_ROOT" not in path.read_text(encoding="utf-8")
    assert "LANGFUSE_SECRET_KEY" in server.env_vars
