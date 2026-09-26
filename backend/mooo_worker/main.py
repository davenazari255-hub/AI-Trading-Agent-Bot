"""Agent Worker entry point.

The worker is a set of supervised asyncio tasks. In the ``live`` role it holds the
single-instance Redis lock. In the ``backtest`` role it never takes the lock and
never touches live state. Trading components are added by later work orders and
must check :attr:`WorkerLock.held` before any trading action.
"""

import asyncio
import logging
import signal
from contextlib import suppress
from typing import Any

from redis.asyncio import Redis

from mooo_core import __version__
from mooo_core.config import Settings, WorkerRole, load_settings_or_exit
from mooo_core.logging_setup import configure_logging
from mooo_worker.health import HealthServer
from mooo_worker.lock import WorkerLock
from mooo_worker.supervisor import TaskSupervisor

logger = logging.getLogger("mooo_worker")


async def report_repeated_crash(name: str, count: int, error: BaseException) -> None:
    """Report repeated task crashes as an ERROR event.

    EventRecord persistence arrives with the data model. Until then the event is
    written to the ERROR log.
    """
    logger.error(
        "ERROR event: supervised task %s crashed %d times (%s)",
        name,
        count,
        type(error).__name__,
    )


def build_health_payload(
    settings: Settings, supervisor: TaskSupervisor, lock: WorkerLock | None
) -> dict[str, Any]:
    return {
        "status": "ok" if supervisor.running else "stopping",
        "service": "worker",
        "role": settings.worker_role.value,
        "version": __version__,
        "bybit_env": settings.bybit_env.value,
        "lock_held": lock.held if lock is not None else None,
        "tasks": supervisor.status(),
    }


async def run_worker(settings: Settings, stop_event: asyncio.Event | None = None) -> None:
    """Run the worker until ``stop_event`` is set."""
    stop = stop_event if stop_event is not None else asyncio.Event()
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
        health_check_interval=30,
    )
    supervisor = TaskSupervisor(on_repeated_crash=report_repeated_crash)

    lock: WorkerLock | None = None
    if settings.worker_role is WorkerRole.LIVE:
        lock = WorkerLock(
            redis,
            ttl_seconds=settings.worker_lock_ttl_seconds,
            heartbeat_interval=settings.worker_lock_heartbeat_seconds,
        )
        supervisor.add("worker-lock", lock.maintain)

    health = HealthServer(
        lambda: build_health_payload(settings, supervisor, lock),
        port=settings.worker_health_port,
    )
    supervisor.add("health-server", health.run)

    supervisor.start()
    logger.info(
        "Mooo worker started (role=%s, bybit_env=%s)",
        settings.worker_role.value,
        settings.bybit_env.value,
    )
    try:
        await stop.wait()
    finally:
        logger.info("Mooo worker stopping")
        await supervisor.stop()
        if lock is not None:
            await lock.release()
        await redis.aclose()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _amain(settings: Settings) -> None:
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    await run_worker(settings, stop)


def main() -> int:
    settings = load_settings_or_exit()
    configure_logging(settings.log_level)
    asyncio.run(_amain(settings))
    return 0
