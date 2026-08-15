"""Live GitHub API adapter stub for later waves."""

from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.connectors.github.scan import GitHubScanItem
from openflywheel.contracts.enums import ConnectorKind
from openflywheel.contracts.source import ConnectorCapabilityReport


class LiveGitHubClientStub:
    """Placeholder for live GitHub integration behind OFW_GITHUB_TOKEN."""

    def capability_report(self) -> ConnectorCapabilityReport:
        return ConnectorCapabilityReport(
            connector_kind=ConnectorKind.GITHUB,
            available=False,
            collections=tuple(),
            historical_bootstrap=False,
            incremental_updates=False,
            identity_fidelity=False,
            stable_source_ids=False,
            notes="Live GitHub adapter not implemented in wave C",
        )

    def list_scan_items(self) -> tuple[GitHubScanItem, ...]:
        return tuple()

    def list_file_envelopes(self) -> tuple[ConnectorEnvelope, ...]:
        return tuple()
