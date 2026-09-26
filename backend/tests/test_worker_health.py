import asyncio
import json

from mooo_core.config import BybitEnv, WorkerRole
from mooo_worker.health import start_health_server
from mooo_worker.state import WorkerState


async def _request(port: int, path: str) -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body


async def test_worker_health_endpoint_reports_state() -> None:
    state = WorkerState(role=WorkerRole.BACKTEST, trading_environment=BybitEnv.DEMO)
    server = await start_health_server(state, host="127.0.0.1", port=0)
    try:
        port = server.sockets[0].getsockname()[1]
        head, body = await _request(port, "/health")
        missing_head, _ = await _request(port, "/nope")
    finally:
        server.close()
        await server.wait_closed()

    assert head.startswith(b"HTTP/1.1 200")
    data = json.loads(body)
    assert data["service"] == "worker"
    assert data["role"] == "backtest"
    assert data["trading_environment"] == "demo"
    assert data["trading_enabled"] is False
    assert missing_head.startswith(b"HTTP/1.1 404")
