"""Shared platform installer protocol and registry."""

from __future__ import annotations

from typing import Protocol

from openflywheel.contracts.enums import PlatformKind
from openflywheel.contracts.operation_result import OperationResult
from openflywheel.contracts.platform import (
    InstallDiagnostics,
    InstallSummary,
    PlatformCapability,
    UninstallSummary,
)


class PlatformInstaller(Protocol):
    @property
    def capability(self) -> PlatformCapability: ...

    def install(
        self,
        *,
        target_home: str,
        project_root: str,
    ) -> OperationResult[InstallSummary]: ...

    def uninstall(
        self,
        *,
        target_home: str,
        project_root: str,
    ) -> OperationResult[UninstallSummary]: ...

    def diagnostics(
        self, *, target_home: str, project_root: str
    ) -> OperationResult[InstallDiagnostics]: ...


_GENERATED_MARKER = "openflywheel-generated"
_SKILL_DIR = "openflywheel"


def generated_marker(platform: PlatformKind) -> str:
    return f"# {_GENERATED_MARKER}:{platform.value}"


def is_generated_block(text: str, platform: PlatformKind) -> bool:
    return generated_marker(platform) in text


def skill_content(platform: PlatformKind) -> str:
    plat = platform.value
    return f"""{generated_marker(platform)}

# OpenFlyWheel System Book

Use OpenFlyWheel book verbs for verified context and write-back.

## Context retrieval

Before answering cross-repo architecture questions, retrieve verified context:

```bash
ofw book context "<query>" --home "$OFW_HOME" --identity "$OFW_IDENTITY"
```

## Write-back

Session lifecycle is recorded via platform hooks.
Episodes are admitted; claims require verification.

Platform: {plat}
"""


def cursor_rule_content() -> str:
    marker = generated_marker(PlatformKind.CURSOR)
    return f"""---
description: OpenFlyWheel System Book context retrieval
alwaysApply: true
---

{marker}

When answering questions about system architecture, memory, data flow, or cross-repo behavior:

1. Run `ofw book context "<query>" --home "$OFW_HOME" --identity "$OFW_IDENTITY"` first.
2. Prefer verified claims and file:line anchors from the packet.
3. If the packet lists gaps, say what is unknown instead of inventing detail.
4. Do not treat proposals or episodes as verified claims.
"""
