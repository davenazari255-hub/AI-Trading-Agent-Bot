"""Supervised asyncio tasks with exponential restart backoff."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

TaskFactory = Callable[[], Awaitable[None]]
CrashReporter = Callable[[str, BaseException, int], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]

_MAX_EXPONENT = 30


class TaskSupervisor:
    """Starts, monitors, and restarts named tasks.

    One failing task never stops the others. A task that crashes is restarted
    after an exponential backoff. When a task crashes ``repeated_crash_threshold``
    times in a row, ``on_repeated_crash`` is called (used to emit ERROR events).
    """

    def __init__(
        self,
        *,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 60.0,
        stable_after_s: float = 60.0,
        repeated_crash_threshold: int = 3,
        on_repeated_crash: CrashReporter | None = None,
        sleep: SleepFn = asyncio.sleep,
        clock: ClockFn = time.monotonic,
    ) -> None:
        if base_backoff_s <= 0:
            raise ValueError("base_backoff_s must be positive")
        if max_backoff_s < base_backoff_s:
            raise ValueError("max_backoff_s must be at least base_backoff_s")
        if repeated_crash_threshold < 1:
            raise ValueError("repeated_crash_threshold must be at least 1")
        self._base_backoff_s = base_backoff_s
        self._max_backoff_s = max_backoff_s
        self._stable_after_s = stable_after_s
        self._threshold = repeated_crash_threshold
        self._on_repeated_crash = on_repeated_crash
        self._sleep = sleep
        self._clock = clock
        self._factories: dict[str, TaskFactory] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()
        self._running = False
        self.crash_counts: dict[str, int] = {}

    @property
    def task_names(self) -> list[str]:
        return list(self._factories)

    def add(self, name: str, factory: TaskFactory) -> None:
        if self._running:
            raise RuntimeError("cannot add tasks after the supervisor has started")
        if name in self._factories:
            raise ValueError(f"task {name!r} is already registered")
        self._factories[name] = factory

    def backoff_delay(self, consecutive_failures: int) -> float:
        exponent = min(max(consecutive_failures - 1, 0), _MAX_EXPONENT)
        return float(min(self._max_backoff_s, self._base_backoff_s * (2**exponent)))

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        """Run all tasks until :meth:`request_stop` is called."""
        if self._running:
            raise RuntimeError("supervisor is already running")
        self._running = True
        self._tasks = {
            name: asyncio.create_task(self._supervise(name, factory), name=f"supervised:{name}")
            for name, factory in self._factories.items()
        }
        try:
            await self._stopping.wait()
        finally:
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._running = False

    async def _supervise(self, name: str, factory: TaskFactory) -> None:
        consecutive_failures = 0
        while not self._stopping.is_set():
            started = self._clock()
            try:
                await factory()
            except Exception as exc:
                if self._clock() - started >= self._stable_after_s:
                    consecutive_failures = 0
                consecutive_failures += 1
                self.crash_counts[name] = self.crash_counts.get(name, 0) + 1
                logger.exception(
                    "Supervised task %r crashed (%d in a row)", name, consecutive_failures
                )
                if consecutive_failures >= self._threshold:
                    await self._report(name, exc, consecutive_failures)
                delay = self.backoff_delay(consecutive_failures)
            else:
                logger.warning("Supervised task %r exited. Restarting it.", name)
                consecutive_failures = 0
                delay = self._base_backoff_s
            if self._stopping.is_set():
                break
            await self._sleep(delay)

    async def _report(self, name: str, exc: BaseException, failures: int) -> None:
        if self._on_repeated_crash is None:
            return
        try:
            await self._on_repeated_crash(name, exc, failures)
        except Exception:
            logger.exception("Crash reporter failed for task %r", name)
