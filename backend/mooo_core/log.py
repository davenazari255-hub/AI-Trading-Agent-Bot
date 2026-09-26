"""Structured JSON logging (Event Bus and Observability blueprint, StructuredLogger).

``configure_logging`` sends every log entry of the process, from structlog and from the
standard ``logging`` module, to stdout as one JSON object per line with ``timestamp``,
``level``, ``category``, and ``correlation_id``. The ``SecretRedactor`` runs on every entry
after exception formatting and immediately before rendering, so tracebacks are redacted too.

``correlation_scope`` binds a Correlation Identifier to the current context. Log entries
and Events recorded inside the scope, including in tasks started inside it, share it.
"""

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, Any
from urllib.parse import urlsplit

import structlog
from pydantic import SecretStr
from structlog.typing import EventDict, Processor, WrappedLogger

from mooo_core.config import Settings
from mooo_core.redaction import SecretRedactor, get_redactor

CORRELATION_ID_KEY = "correlation_id"
CATEGORY_KEY = "category"
DEFAULT_CATEGORY = "INFO"

_LEVEL_CATEGORIES = {
    "warn": "WARNING",
    "warning": "WARNING",
    "error": "ERROR",
    "exception": "ERROR",
    "critical": "ERROR",
    "fatal": "ERROR",
}
_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def add_standard_fields(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Make sure every entry has ``category`` and ``correlation_id`` keys."""
    category = event_dict.get(CATEGORY_KEY)
    if category is None:
        level = str(event_dict.get("level", method_name)).lower()
        event_dict[CATEGORY_KEY] = _LEVEL_CATEGORIES.get(level, DEFAULT_CATEGORY)
    else:
        event_dict[CATEGORY_KEY] = str(category)
    correlation_id = event_dict.get(CORRELATION_ID_KEY)
    event_dict[CORRELATION_ID_KEY] = None if correlation_id is None else str(correlation_id)
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_standard_fields,
    ]


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(level.strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def configure_logging(
    level: str | int = "INFO",
    *,
    redactor: SecretRedactor | None = None,
    stream: IO[str] | None = None,
) -> None:
    """Route all process logs through structlog JSON rendering with secret redaction.

    Replaces the root handlers, so ``logging.basicConfig`` output cannot bypass redaction.
    """
    active_redactor = redactor if redactor is not None else get_redactor()
    shared = _shared_processors()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            active_redactor,
            structlog.processors.JSONRenderer(default=str),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(_resolve_level(level))
    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True


def register_settings_secrets(settings: Settings, redactor: SecretRedactor) -> int:
    """Register deployment secrets (master key, AI key, URL passwords) for redaction."""
    values: list[str | SecretStr | None] = [
        settings.mooo_master_key,
        settings.anthropic_api_key,
    ]
    for url in (settings.database_url, settings.redis_url):
        try:
            values.append(urlsplit(url).password)
        except ValueError:
            continue
    return sum(1 for value in values if redactor.register_secret(value))


def configure_process_logging(settings: Settings) -> None:
    """Container entry point: register deployment secrets, then configure JSON logging."""
    redactor = get_redactor()
    register_settings_secrets(settings, redactor)
    configure_logging(settings.log_level, redactor=redactor)


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger. Keyword arguments on log calls become JSON fields."""
    return structlog.stdlib.get_logger(name) if name else structlog.stdlib.get_logger()


def new_correlation_id() -> uuid.UUID:
    return uuid.uuid4()


def coerce_correlation_id(value: uuid.UUID | str | None) -> uuid.UUID | None:
    """Return a UUID, or raise ValueError when a string is not a valid UUID."""
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def current_correlation_id() -> uuid.UUID | None:
    """Return the Correlation Identifier bound to the current context, if any."""
    value = structlog.contextvars.get_contextvars().get(CORRELATION_ID_KEY)
    if value is None:
        return None
    try:
        return coerce_correlation_id(value)
    except ValueError:
        return None


@contextmanager
def correlation_scope(correlation_id: uuid.UUID | str | None = None) -> Iterator[uuid.UUID]:
    """Bind a Correlation Identifier (new when omitted) to logs and Events in this scope."""
    resolved = coerce_correlation_id(correlation_id) or new_correlation_id()
    with structlog.contextvars.bound_contextvars(**{CORRELATION_ID_KEY: str(resolved)}):
        yield resolved
