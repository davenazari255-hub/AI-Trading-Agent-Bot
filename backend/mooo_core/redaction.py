"""Secret redaction for every log entry and every Event (Credential Vault blueprint).

``SecretRedactor`` removes:

* the value of every field whose name looks like a credential: names that contain
  ``secret``, ``password``, ``passphrase``, ``cookie``, ``authorization``, or ``apikey``;
  names with a ``key``, ``token``, ``pwd``, ``sign``, or ``signature`` segment; and Bybit
  ``X-BAPI-*`` headers;
* credential-looking ``name=value`` and ``name: value`` pairs, ``X-BAPI-*`` headers,
  cookies, and ``Authorization`` headers inside free text;
* every secret value registered at runtime, wherever it appears inside a string.

One instance is a structlog processor (see ``mooo_core.log``). The ``EventRecorder``
applies it to the message, symbol, and refs of every Event.
"""

import re
import threading
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import SecretBytes, SecretStr
from structlog.typing import EventDict, WrappedLogger

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

MIN_SECRET_LENGTH = 6
"""Registered values shorter than this are ignored so that common words are not replaced."""

MAX_DEPTH = 16

NON_SECRET_FIELD_NAMES = frozenset(
    {"masked_key", "key_version", "key_fingerprint", "api_key_fingerprint"}
)
"""Credential Vault metadata that is safe to show: the masked key (last 4 characters), the
master key version, and the SHA-256 fingerprint of the api_key."""

_SUBSTRING_MARKERS = (
    "secret",
    "password",
    "passwd",
    "passphrase",
    "cookie",
    "authorization",
    "apikey",
)
_SEGMENT_MARKERS = frozenset({"key", "token", "pwd", "sign", "signature"})
_HEADER_PREFIXES = ("x-bapi-", "x_bapi_")
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")

_NOT_ALREADY_REDACTED = r"(?!\s*\[REDACTED\])"
_TEXT_RULES: tuple[re.Pattern[str], ...] = (
    # Bybit signed-request headers such as X-BAPI-API-KEY and X-BAPI-SIGN.
    re.compile(
        r"(?i)(?<![a-z0-9])(x-bapi-[a-z0-9-]+[\"']?\s*[:=]\s*[\"']?)"
        + _NOT_ALREADY_REDACTED
        + r"[^\s\"',;&}\]]+"
    ),
    # Cookie, Set-Cookie, and Authorization values run to the end of the line.
    re.compile(
        r"(?i)(?<![a-z0-9])((?:set-)?cookie[\"']?\s*[:=]\s*|authorization[\"']?\s*[:=]\s*)"
        + _NOT_ALREADY_REDACTED
        + r"[^\r\n]+"
    ),
    # name=value and "name": "value" pairs for credential-like names.
    re.compile(
        r"(?i)(?<![a-z0-9])((?:api[_-]?key|api[_-]?secret|secret(?:[_-]?key)?|password|passwd"
        r"|passphrase|(?:access|refresh|session|auth)[_-]?token|token|sign|signature)"
        r"[\"']?\s*[:=]\s*[\"']?)"
        + _NOT_ALREADY_REDACTED
        + r"[^\s\"'&,;}\]]+"
    ),
)


def is_sensitive_name(name: str) -> bool:
    """Return True when a field or header name looks like it holds a credential."""
    stripped = name.strip()
    lowered = stripped.lower()
    if not lowered or lowered in NON_SECRET_FIELD_NAMES:
        return False
    if lowered.startswith(_HEADER_PREFIXES):
        return True
    if any(marker in lowered for marker in _SUBSTRING_MARKERS):
        return True
    segments = _SEGMENT_SPLIT.split(_CAMEL_BOUNDARY.sub(r"\1_\2", stripped).lower())
    return any(segment in _SEGMENT_MARKERS for segment in segments)


def _secret_text(value: str | SecretStr | SecretBytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, SecretStr):
        text = value.get_secret_value()
    elif isinstance(value, SecretBytes):
        text = value.get_secret_value().decode("utf-8", errors="ignore")
    else:
        text = value
    text = text.strip()
    return text or None


def _mask(value: Any) -> Any:
    return None if value is None else REDACTED


def _keep_prefix(match: re.Match[str]) -> str:
    return match.group(1) + REDACTED


class SecretRedactor:
    """Removes credentials from strings, nested structures, and structlog event dicts."""

    def __init__(self, secrets: Iterable[str | SecretStr] = ()) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        for secret in secrets:
            self.register_secret(secret)

    @property
    def registered_count(self) -> int:
        return len(self._secrets)

    def register_secret(self, value: str | SecretStr | SecretBytes | None) -> bool:
        """Register a decrypted secret so any occurrence in output becomes [REDACTED].

        Returns False when the value is empty or shorter than ``MIN_SECRET_LENGTH``.
        """
        text = _secret_text(value)
        if text is None or len(text) < MIN_SECRET_LENGTH:
            return False
        with self._lock:
            if text not in self._secrets:
                self._secrets.add(text)
                self._rebuild()
        return True

    def unregister_secret(self, value: str | SecretStr | SecretBytes | None) -> None:
        text = _secret_text(value)
        if text is None:
            return
        with self._lock:
            if text in self._secrets:
                self._secrets.discard(text)
                self._rebuild()

    def clear_secrets(self) -> None:
        with self._lock:
            self._secrets.clear()
            self._rebuild()

    def _rebuild(self) -> None:
        if not self._secrets:
            self._pattern = None
            return
        ordered = sorted(self._secrets, key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(secret) for secret in ordered))

    def redact_text(self, text: str) -> str:
        """Replace registered secrets and credential-looking pairs inside free text."""
        pattern = self._pattern
        if pattern is not None:
            text = pattern.sub(REDACTED, text)
        for rule in _TEXT_RULES:
            text = rule.sub(_keep_prefix, text)
        return text

    def redact(self, value: Any) -> Any:
        """Return a redacted copy of any value. Unknown objects become redacted strings."""
        return self._redact(value, 0, set())

    def redact_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return self._redact_mapping(value, 0, set())

    def __call__(
        self, logger: WrappedLogger, method_name: str, event_dict: EventDict
    ) -> EventDict:
        """structlog processor interface."""
        return self.redact_mapping(event_dict)

    def _redact(self, value: Any, depth: int, seen: set[int]) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, SecretStr | SecretBytes):
            return REDACTED
        if isinstance(value, bytes | bytearray):
            return f"<{len(value)} bytes redacted>"
        if isinstance(value, datetime | date | time | UUID | Decimal):
            return value
        if depth >= MAX_DEPTH or id(value) in seen:
            return TRUNCATED
        if isinstance(value, Mapping):
            return self._redact_mapping(value, depth, seen)
        if isinstance(value, list | tuple | set | frozenset):
            seen.add(id(value))
            try:
                items = [self._redact_item(item, depth + 1, seen) for item in value]
            finally:
                seen.discard(id(value))
            return tuple(items) if isinstance(value, tuple) else items
        return self.redact_text(str(value))

    def _redact_item(self, item: Any, depth: int, seen: set[int]) -> Any:
        # Header lists such as [("X-BAPI-SIGN", "..."), ("Accept", "...")].
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and is_sensitive_name(item[0])
        ):
            return (item[0], _mask(item[1]))
        return self._redact(item, depth, seen)

    def _redact_mapping(
        self, value: Mapping[Any, Any], depth: int, seen: set[int]
    ) -> dict[Any, Any]:
        seen.add(id(value))
        try:
            result: dict[Any, Any] = {}
            for key, item in value.items():
                safe_key = self.redact_text(key) if isinstance(key, str) else key
                if isinstance(key, str) and is_sensitive_name(key):
                    result[safe_key] = _mask(item)
                else:
                    result[safe_key] = self._redact(item, depth + 1, seen)
            return result
        finally:
            seen.discard(id(value))


_default_redactor = SecretRedactor()


def get_redactor() -> SecretRedactor:
    """Return the process-wide redactor shared by logging and the EventRecorder."""
    return _default_redactor
