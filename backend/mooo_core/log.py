"""Structured JSON logging with secret redaction (Event Bus and Observability blueprint).

``configure_logging`` sets up structlog and the standard library root logger. Both write
one JSON object per line to stdout with timestamp, level, category, and correlation_id,
and both pass every record through the SecretRedactor.
"""

import json
import logging
import sys
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import UUID

import structlog
from structlog.typing import Processor

from mooo_core.redaction import SecretRedactor, default_redactor

DEFAULT_CATEGORY = "INFO"
_HANDLER_MARK = "_mooo_handler"


def _add_standard_fields(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict.setdefault("category", DEFAULT_CATEGORY)
    event_dict.setdefault("correlation_id", None)
    return event_dict


class RedactingLogFilter(logging.Filter):
    """Redacts standard library records (uvicorn, httpx, SQLAlchemy) before output."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact_text(record.getMessage())
        record.args = None
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._redactor.redact_text(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redactor.redact_text(record.exc_text)
        return True


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "category": getattr(record, "category", DEFAULT_CATEGORY),
            "correlation_id": getattr(record, "correlation_id", None),
            "event": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, default=str)


def configure_logging(
    level: str = "INFO",
    *,
    redactor: SecretRedactor | None = None,
    stream: TextIO | None = None,
) -> None:
    active = redactor or default_redactor
    output = stream or sys.stdout
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_standard_fields,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        active,
        structlog.processors.JSONRenderer(default=str),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=output),
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
    handler = logging.StreamHandler(output)
    setattr(handler, _HANDLER_MARK, True)
    handler.addFilter(RedactingLogFilter(active))
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)
    root.setLevel(numeric_level)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def bind_correlation_id(correlation_id: UUID | str | None) -> None:
    value = str(correlation_id) if correlation_id is not None else None
    structlog.contextvars.bind_contextvars(correlation_id=value)


def clear_correlation_id() -> None:
    structlog.contextvars.unbind_contextvars("correlation_id")
