"""Secret redaction for logs and events (Credential Vault blueprint, SecretRedactor).

Three layers keep credentials out of every log line and ``EventRecord``:

* Field names: values under names such as ``api_key``, ``secret``, ``token``, ``password``,
  ``cookie``, or any ``X-BAPI-*`` header are replaced.
* Registered values: decrypted secrets are registered at runtime, and any string that
  contains one has it replaced.
* Inline patterns: ``name=value`` and ``Name: value`` pairs for secret names, and bearer
  tokens, are replaced inside free text.
"""

import re
import threading
from collections.abc import Mapping, MutableMapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import SecretBytes, SecretStr

REDACTED = "[REDACTED]"
MIN_REGISTERED_LENGTH = 4
MAX_DEPTH = 20

SENSITIVE_NAME_PARTS = frozenset(
    {
        "secret",
        "key",
        "apikey",
        "token",
        "password",
        "passwd",
        "passphrase",
        "cookie",
        "authorization",
        "sign",
        "signature",
        "credential",
        "credentials",
        "session",
    }
)
# Names that contain a sensitive part but only hold non-secret data.
SAFE_NAMES = frozenset({"masked_key", "key_fingerprint", "key_version", "dedupe_key"})

_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_NAME_SEPARATORS = re.compile(r"[^a-z0-9]+")
_INLINE_SECRET = re.compile(
    r"(?i)\b(x-bapi-[a-z-]+|api[_-]?key|api[_-]?secret|secret|sign|signature|"
    r"(?:access|refresh|session)?[_-]?token|password|set-cookie|cookie)"
    r"(\s*[:=]\s*)([\"']?)([^\s\"',;&]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]+")
_SCALARS: tuple[type, ...] = (bool, int, float, Decimal, datetime, date, UUID, Enum, type(None))


def is_sensitive_name(name: str) -> bool:
    """Return True when a field or header name usually holds a secret."""
    stripped = name.strip()
    lowered = stripped.lower()
    if lowered in SAFE_NAMES:
        return False
    if lowered.startswith("x-bapi-"):
        return True
    parts = _NAME_SEPARATORS.split(_CAMEL_BOUNDARY.sub(r"\1_\2", stripped).lower())
    return any(part in SENSITIVE_NAME_PARTS for part in parts)


class SecretRedactor:
    """Removes secrets from text and nested structures. Also a structlog processor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()
        self._ordered: tuple[str, ...] = ()

    def register(self, *values: str | SecretStr | None) -> None:
        """Register decrypted secret values so any accidental match is replaced."""
        with self._lock:
            for value in values:
                raw = value.get_secret_value() if isinstance(value, SecretStr) else value
                if raw and len(raw) >= MIN_REGISTERED_LENGTH:
                    self._values.add(raw)
            self._ordered = tuple(sorted(self._values, key=len, reverse=True))

    def unregister(self, *values: str | SecretStr | None) -> None:
        with self._lock:
            for value in values:
                raw = value.get_secret_value() if isinstance(value, SecretStr) else value
                if raw:
                    self._values.discard(raw)
            self._ordered = tuple(sorted(self._values, key=len, reverse=True))

    def redact_text(self, text: str) -> str:
        result = str(text)
        for value in self._ordered:
            if value in result:
                result = result.replace(value, REDACTED)
        result = _INLINE_SECRET.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}", result
        )
        return _BEARER.sub(lambda match: f"{match.group(1)} {REDACTED}", result)

    def redact(self, value: Any, _depth: int = 0) -> Any:
        """Return a copy of ``value`` with every secret replaced by ``[REDACTED]``."""
        if _depth > MAX_DEPTH:
            return REDACTED
        if isinstance(value, SecretStr | SecretBytes):
            return REDACTED
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, bytes | bytearray):
            return f"<{len(value)} bytes>"
        if isinstance(value, _SCALARS):
            return value
        if isinstance(value, Mapping):
            return {key: self._redact_item(key, item, _depth) for key, item in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self.redact(item, _depth + 1) for item in value]
        return self.redact_text(str(value))

    def _redact_item(self, key: Any, value: Any, depth: int) -> Any:
        if value is not None and isinstance(key, str) and is_sensitive_name(key):
            return REDACTED
        return self.redact(value, depth + 1)

    def __call__(
        self, logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        for key in list(event_dict):
            event_dict[key] = self._redact_item(key, event_dict[key], 0)
        return event_dict


default_redactor = SecretRedactor()
