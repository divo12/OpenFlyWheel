"""Cursor connect stub."""

from openflywheel.connectors.agents.base import cursor_capability_report
from openflywheel.contracts.source import ConnectorCapabilityReport


def report() -> ConnectorCapabilityReport:
    return cursor_capability_report()
