"""GitHub client protocol."""

from typing import Protocol

from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.connectors.github.scan import GitHubScanItem
from openflywheel.contracts.source import ConnectorCapabilityReport


class GitHubClient(Protocol):
    def capability_report(self) -> ConnectorCapabilityReport: ...

    def list_scan_items(self) -> tuple[GitHubScanItem, ...]: ...

    def list_file_envelopes(self) -> tuple[ConnectorEnvelope, ...]: ...
