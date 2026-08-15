"""Agent connect capability stubs."""

from openflywheel.contracts.enums import ConnectorKind
from openflywheel.contracts.source import ConnectorCapabilityReport


def claude_capability_report() -> ConnectorCapabilityReport:
    return ConnectorCapabilityReport(
        connector_kind=ConnectorKind.CLAUDE_CODE,
        available=True,
        collections=("sessions", "hooks"),
        historical_bootstrap=True,
        incremental_updates=True,
        identity_fidelity=True,
        stable_source_ids=True,
        notes="Claude Code connect stub for onboarding wave B",
    )


def cursor_capability_report() -> ConnectorCapabilityReport:
    return ConnectorCapabilityReport(
        connector_kind=ConnectorKind.CURSOR,
        available=True,
        collections=("sessions", "rules", "hooks"),
        historical_bootstrap=True,
        incremental_updates=True,
        identity_fidelity=True,
        stable_source_ids=True,
        notes="Cursor connect stub for onboarding wave B",
    )
