"""Minimal HTTP health endpoint for the Agent Worker (``GET /health``)."""

import asyncio
import contextlib
import json
from typing import Any

from mooo_worker.state import WorkerState

_READ_TIMEOUT_S = 5.0
_MAX_HEADER_LINES = 100


async def _handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: WorkerState
) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT_S)
        for _ in range(_MAX_HEADER_LINES):
            line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT_S)
            if line in (b"\r\n", b"\n", b""):
                break
        parts = request_line.decode("latin-1").split()
        method = parts[0] if parts else ""
        path = parts[1] if len(parts) > 1 else ""
        body: dict[str, Any]
        if method == "GET" and path == "/health":
            status = "200 OK"
            body = state.snapshot()
        else:
            status = "404 Not Found"
            body = {"error_code": "not_found", "message": "Not found"}
        payload = json.dumps(body).encode()
        head = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode() + payload)
        await writer.drain()
    except (TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def start_health_server(
    state: WorkerState, *, host: str = "0.0.0.0", port: int = 8081
) -> asyncio.Server:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle(reader, writer, state)

    return await asyncio.start_server(handler, host=host, port=port)


async def serve_health(state: WorkerState, *, host: str = "0.0.0.0", port: int = 8081) -> None:
    server = await start_health_server(state, host=host, port=port)
    async with server:
        await server.serve_forever()
