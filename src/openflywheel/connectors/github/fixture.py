"""GitHub fixture filesystem adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.connectors.github.scan import GitHubScanItem, ScanItemKind, UnsupportedFileItem
from openflywheel.contracts.acl import AclLabel
from openflywheel.contracts.enums import ConnectorKind, VisibilityLevel
from openflywheel.contracts.source import ConnectorCapabilityReport


class FixtureGitHubClient:
    def __init__(self, root: Path) -> None:
        self._root = root

    def capability_report(self) -> ConnectorCapabilityReport:
        return ConnectorCapabilityReport(
            connector_kind=ConnectorKind.GITHUB,
            available=True,
            collections=("files", "issues", "pull_requests", "commits"),
            historical_bootstrap=True,
            incremental_updates=True,
            identity_fidelity=True,
            stable_source_ids=True,
            notes="Fixture filesystem adapter for offline ingest",
        )

    def list_scan_items(self) -> tuple[GitHubScanItem, ...]:
        items: list[GitHubScanItem] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self._root).as_posix()
            if rel.startswith(".git/"):
                continue
            stat = path.stat()
            event_time = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                items.append(
                    GitHubScanItem(
                        kind=ScanItemKind.UNSUPPORTED,
                        unsupported=UnsupportedFileItem(
                            external_id=rel,
                            uri=f"fixture://{rel}",
                            detail="binary or undecodable file",
                            event_time=event_time,
                        ),
                    )
                )
                continue
            items.append(
                GitHubScanItem(
                    kind=ScanItemKind.FILE,
                    envelope=ConnectorEnvelope(
                        external_id=rel,
                        uri=f"fixture://{rel}",
                        content_text=content,
                        content_type="text/plain",
                        event_time=event_time,
                        acl=AclLabel(visibility=VisibilityLevel.INTERNAL),
                    ),
                )
            )
        return tuple(items)

    def list_file_envelopes(self) -> tuple[ConnectorEnvelope, ...]:
        envelopes: list[ConnectorEnvelope] = []
        for item in self.list_scan_items():
            if item.kind == ScanItemKind.FILE and item.envelope is not None:
                envelopes.append(item.envelope)
        return tuple(envelopes)
