"""Wire formats shared by the API Server and the Agent Worker over Redis.

``EventMessage`` is the JSON form of one ``EventRecord``. The same shape is written to
PostgreSQL and published to ``mooo:events`` (Event Log ADR-001). ``StreamMessage`` is the
frame that the StreamGateway forwards to the dashboard: ``{type, environment, ts, payload}``.
"""

import uuid
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from mooo_core.models import Environment, EventCategory

STREAM_TYPE_EVENT = "event"


class EventMessage(BaseModel):
    """One categorized audit Event. Already redacted when created by the EventRecorder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Environment
    category: EventCategory
    message: str
    symbol: str | None = None
    correlation_id: uuid.UUID | None = None
    refs: dict[str, Any] | None = None
    ts: AwareDatetime


class StreamMessage(BaseModel):
    """Frame published on Redis pub/sub channels and forwarded to the dashboard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(min_length=1)
    environment: Environment
    ts: AwareDatetime
    payload: dict[str, Any]

    @classmethod
    def for_event(cls, event: EventMessage) -> Self:
        return cls(
            type=STREAM_TYPE_EVENT,
            environment=event.environment,
            ts=event.ts,
            payload=event.model_dump(mode="json"),
        )
