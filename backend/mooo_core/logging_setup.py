"""Logging setup shared by the API Server and the Agent Worker."""

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging once per process.

    Log messages must never contain secrets. Settings expose secrets only as
    ``SecretStr`` and hide URL fields from ``repr`` to support this.
    """
    logging.basicConfig(level=level.upper(), format=_FORMAT, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
