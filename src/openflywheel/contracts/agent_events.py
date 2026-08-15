"""Canonical agent event contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from openflywheel.contracts.enums import AgentEventKind
from openflywheel.contracts.ids import AgentSessionId


class AgentEventRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: AgentSessionId
    kind: AgentEventKind
    text: str
    event_time: datetime
