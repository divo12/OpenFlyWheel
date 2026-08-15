"""Connector protocols."""

from typing import Protocol

from openflywheel.connectors.envelope import ConnectorEnvelope
from openflywheel.contracts.source import ConnectorCapabilityReport


class ConnectorClient(Protocol):
    def capability_report(self) -> ConnectorCapabilityReport: ...

    def list_envelopes(self) -> tuple[ConnectorEnvelope, ...]: ...
