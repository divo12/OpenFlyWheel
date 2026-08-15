"""Platform installer contracts."""

from pydantic import BaseModel, ConfigDict, Field

from openflywheel.contracts.enums import PlatformKind


class PlatformCapability(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: PlatformKind
    supports_hooks: bool
    supports_skills: bool
    supports_rules: bool
    supports_mcp: bool
    transcript_format: str
    config_paths: tuple[str, ...] = Field(default_factory=tuple)


class InstallArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    action: str


class InstallDiagnostics(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: PlatformKind
    installed: bool
    artifacts: tuple[InstallArtifact, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = Field(default_factory=tuple)


class InstallSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: PlatformKind
    installed_paths: tuple[str, ...] = Field(default_factory=tuple)
    merged_files: tuple[str, ...] = Field(default_factory=tuple)
    skipped_existing: tuple[str, ...] = Field(default_factory=tuple)


class UninstallSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: PlatformKind
    removed_paths: tuple[str, ...] = Field(default_factory=tuple)
    restored_files: tuple[str, ...] = Field(default_factory=tuple)
