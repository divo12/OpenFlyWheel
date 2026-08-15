"""Expert notes connect stub."""

from openflywheel.contracts.enums import ConnectorKind
from openflywheel.contracts.source import ConnectorCapabilityReport


def notes_capability_report() -> ConnectorCapabilityReport:
    return ConnectorCapabilityReport(
        connector_kind=ConnectorKind.EXPERT_NOTES,
        available=True,
        collections=("markdown_files",),
        historical_bootstrap=True,
        incremental_updates=True,
        identity_fidelity=False,
        stable_source_ids=True,
        notes="Expert notes folder stub",
    )
