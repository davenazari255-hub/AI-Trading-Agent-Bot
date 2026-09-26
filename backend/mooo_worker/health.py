"""Minimal HTTP health endpoint for the Agent Worker.

The worker has no web framework. This server answers ``GET /health`` with a JSON
status document so Docker health checks and operators can inspect the process.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

HealthProvider = Callable[[], dict[str, Any]]

_MAX_REQUEST_BYTES = 8192
_READ_TIMEOUT_SECONDS = 5.0
_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
}


def _error(error_code: str, message: str) -> dict[str, Any]:
    return {"error_code": error_code, "message": message}


class HealthServer:
    def __init__(
        self,
        provider: HealthProvider,
        *,
        host: str = "0.0.0.0",
        port: int = 8081,
    ) -> None:
        self._provider = provider
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._ready = asyncio.Event()

    @property
    def port(self) -> int | None:
        """The bound port, or None when the server is not listening."""
        if self._server is None or not self._server.sockets:
            return None
        return int(self._server.sockets[0].getsockname()[1])

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def run(self) -> None:
        """Listen until cancelled. Intended to run under the TaskSupervisor."""
        server = await asyncio.start_server(self._handle, self._host, self._port)
        self._server = server
        self._ready.set()
        logger.info("Worker health endpoint listening on %s:%s", self._host, self.port)
        try:
            async with server:
                await server.serve_forever()
        finally:
            self._server = None
            self._ready.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await self._read_request_line(reader)
            if request_line is None:
                return
            status, payload, head_only = self._route(request_line)
            await self._respond(writer, status, payload, head_only=head_only)
        except Exception:
            logger.exception("Health request failed")
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _read_request_line(self, reader: asyncio.StreamReader) -> str | None:
        first: bytes | None = None
        total = 0
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT_SECONDS)
                total += len(line)
                if total > _MAX_REQUEST_BYTES:
                    return ""
                if first is None:
                    if not line:
                        return None
                    first = line
                    continue
                if line in (b"\r\n", b"\n", b""):
                    break
        except (TimeoutError, ConnectionError, ValueError):
            return None
        return first.decode("latin-1").strip() if first is not None else None

    def _route(self, request_line: str) -> tuple[int, dict[str, Any], bool]:
        parts = request_line.split()
        if len(parts) != 3:
            return 400, _error("bad_request", "Malformed request"), False
        method, target, _version = parts
        if method not in ("GET", "HEAD"):
            return 405, _error("method_not_allowed", "Method not allowed"), False
        head_only = method == "HEAD"
        if target.split("?", 1)[0] != "/health":
            return 404, _error("not_found", "Not found"), head_only
        try:
            payload = self._provider()
        except Exception:
            logger.exception("Health provider failed")
            return 500, _error("internal_error", "Health check failed"), head_only
        return 200, payload, head_only

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
        *,
        head_only: bool,
    ) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        headers = [
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        if status == 405:
            headers.append("Allow: GET, HEAD")
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
        if not head_only:
            writer.write(body)
        await writer.drain()
