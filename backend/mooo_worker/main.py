"""Agent Worker process: supervised tasks, heartbeat, health, and the single-instance lock.

Trading components are added by later work orders. They run as supervised tasks
and may trade only while ``WorkerState.trading_enabled`` is true.
"""

import asyncio
import contextlib
import json
import logging
import signal
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from mooo_core.bus import EVENTS_CHANNEL
from mooo_core.config import Settings, WorkerRole
from mooo_worker.health import serve_health
from mooo_worker.heartbeat import heartbeat_loop
from mooo_worker.lock import WorkerLock, maintain_lock
from mooo_worker.state import WorkerState
from mooo_worker.supervisor import CrashReporter, TaskSupervisor

logger = logging.getLogger(__name__)


def make_crash_reporter(redis: Redis) -> CrashReporter:
    """Publish an ERROR event when a supervised task crashes repeatedly."""

    async def report(name: str, exc: BaseException, failures: int) -> None:
        event = {
            "category": "ERROR",
            "source": "worker",
            "message": f"Task '{name}' crashed {failures} times in a row ({type(exc).__name__})",
            "ts": time.time(),
        }
        try:
            await redis.publish(EVENTS_CHANNEL, json.dumps(event))
        except (RedisError, OSError):
            logger.warning("Could not publish the ERROR event for task %s", name)

    return report


async def run_worker(settings: Settings) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    state = WorkerState(role=settings.worker_role, trading_environment=settings.bybit_env)
    supervisor = TaskSupervisor(on_repeated_crash=make_crash_reporter(redis))
    supervisor.add("health", lambda: serve_health(state, port=settings.worker_health_port))
    supervisor.add("heartbeat", lambda: heartbeat_loop(redis, state))

    lock: WorkerLock | None = None
    if settings.worker_role is WorkerRole.TRADING:
        trading_lock = WorkerLock(redis)
        lock = trading_lock
        supervisor.add("worker-lock", lambda: maintain_lock(trading_lock, state))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, supervisor.request_stop)

    logger.info(
        "Agent Worker starting: role=%s environment=%s live_trading_allowed=%s",
        settings.worker_role.value,
        settings.bybit_env.value,
        settings.allow_live_trading,
    )
    try:
        await supervisor.run()
    finally:
        if lock is not None and lock.held:
            with contextlib.suppress(RedisError, OSError):
                await lock.release()
        await redis.aclose()
        logger.info("Agent Worker stopped")
