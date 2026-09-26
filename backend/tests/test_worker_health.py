import asyncio
import json
from contextlib import suppress

from mooo_worker.health import HealthServer


async def _request(port: int, raw: bytes) -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    await writer.wait_closed()
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


async def _serve(server: HealthServer) -> "asyncio.Task[None]":
    task = asyncio.create_task(server.run())
    await asyncio.wait_for(server.wait_ready(), timeout=5)
    return task


async def _shutdown(task: "asyncio.Task[None]") -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_health_returns_provider_payload() -> None:
    server = HealthServer(lambda: {"status": "ok", "service": "worker"}, host="127.0.0.1", port=0)
    task = await _serve(server)
    try:
        assert server.port is not None
        head, body = await _request(server.port, b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK")
        assert b"Content-Type: application/json" in head
        assert json.loads(body) == {"status": "ok", "service": "worker"}
    finally:
        await _shutdown(task)


async def test_unknown_path_and_method() -> None:
    server = HealthServer(lambda: {"status": "ok"}, host="127.0.0.1", port=0)
    task = await _serve(server)
    try:
        assert server.port is not None
        head, body = await _request(server.port, b"GET /nope HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 404")
        assert json.loads(body) == {"error_code": "not_found", "message": "Not found"}

        head, _body = await _request(server.port, b"POST /health HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 405")
        assert b"Allow: GET, HEAD" in head

        head, _body = await _request(server.port, b"garbage\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 400")
    finally:
        await _shutdown(task)


async def test_provider_failure_returns_500() -> None:
    def broken() -> dict[str, object]:
        raise RuntimeError("provider failed")

    server = HealthServer(broken, host="127.0.0.1", port=0)
    task = await _serve(server)
    try:
        assert server.port is not None
        head, body = await _request(server.port, b"GET /health HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 500")
        assert json.loads(body)["error_code"] == "internal_error"
    finally:
        await _shutdown(task)
