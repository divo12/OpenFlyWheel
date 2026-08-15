"""Claude Code connect stub."""

from openflywheel.connectors.agents.base import claude_capability_report
from openflywheel.contracts.source import ConnectorCapabilityReport


def report() -> ConnectorCapabilityReport:
    return claude_capability_report()
