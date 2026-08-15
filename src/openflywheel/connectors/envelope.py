"""Connector envelope types."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.acl import AclLabel


class ConnectorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    uri: str
    content_text: str
    content_type: str
    event_time: datetime
    acl: AclLabel
