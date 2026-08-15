"""GitHub fixture scan result types."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from openflywheel.connectors.envelope import ConnectorEnvelope


class ScanItemKind(StrEnum):
    FILE = "file"
    UNSUPPORTED = "unsupported"


class UnsupportedFileItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    uri: str
    detail: str
    event_time: datetime


class GitHubScanItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ScanItemKind
    envelope: ConnectorEnvelope | None = None
    unsupported: UnsupportedFileItem | None = None
