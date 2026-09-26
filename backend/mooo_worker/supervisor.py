"""Supervised asyncio tasks with exponential restart backoff.

The Agent Worker is built from independent asyncio tasks. One failing task must
not stop the others, so each task runs under :class:`TaskSupervisor`, which
restarts it with exponential backoff and reports repeated crashes.
"""

import asyncio
import inspect
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

TaskFactory = Callable[[], Awaitable[None]]
CrashReporter = Callable[[str, int, BaseException], Awaitable[None] | None]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]


@dataclass
class TaskStatus:
    name: str
    running: bool = False
    restarts: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "restarts": self.restarts,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class _TaskSpec:
    name: str
    factory: TaskFactory


class TaskSupervisor:
    """Start, monitor, and restart asyncio tasks with exponential backoff.

    * A task that raises is logged and restarted after a backoff delay that doubles
      on each consecutive failure, capped at ``max_backoff``.
    * A task that ran for at least ``healthy_after`` seconds before failing starts
      again from ``initial_backoff``.
    * A task that returns normally is treated as an unexpected exit and restarted.
    * When a task crashes ``crash_threshold`` times within ``crash_window`` seconds,
      the supervisor logs at ERROR and calls ``on_repeated_crash`` so the caller can
      record an ERROR event.
    """

    def __init__(
        self,
        *,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        backoff_multiplier: float = 2.0,
        healthy_after: float = 60.0,
        crash_threshold: int = 3,
        crash_window: float = 300.0,
        on_repeated_crash: CrashReporter | None = None,
        sleep: SleepFn = asyncio.sleep,
        clock: ClockFn = time.monotonic,
    ) -> None:
        if initial_backoff <= 0 or max_backoff < initial_backoff:
            raise ValueError("backoff must satisfy 0 < initial_backoff <= max_backoff")
        if backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1")
        if crash_threshold < 1:
            raise ValueError("crash_threshold must be at least 1")
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._multiplier = backoff_multiplier
        self._healthy_after = healthy_after
        self._crash_threshold = crash_threshold
        self._crash_window = crash_window
        self._on_repeated_crash = on_repeated_crash
        self._sleep = sleep
        self._clock = clock
        self._specs: dict[str, _TaskSpec] = {}
        self._status: dict[str, TaskStatus] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._started and not self._stopping

    def add(self, name: str, factory: TaskFactory) -> None:
        """Register a task. If the supervisor is already running, start it now."""
        if name in self._specs:
            raise ValueError(f"task {name!r} is already registered")
        if self._stopping:
            raise RuntimeError("cannot add tasks to a stopping supervisor")
        spec = _TaskSpec(name=name, factory=factory)
        self._specs[name] = spec
        self._status[name] = TaskStatus(name=name)
        if self._started:
            self._launch(spec)

    def start(self) -> None:
        """Start all registered tasks. Must be called from a running event loop."""
        if self._started:
            return
        self._started = True
        for spec in self._specs.values():
            self._launch(spec)

    async def stop(self, timeout: float = 10.0) -> None:
        """Cancel all tasks and wait for them to finish."""
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                names = ", ".join(sorted(t.get_name() for t in pending))
                logger.warning("Supervised tasks did not stop within %.1f s: %s", timeout, names)
        self._tasks.clear()
        for status in self._status.values():
            status.running = False

    def backoff_for(self, consecutive_failures: int) -> float:
        """Return the restart delay for the given number of consecutive failures."""
        if consecutive_failures <= 0:
            return 0.0
        delay = self._initial_backoff * (self._multiplier ** (consecutive_failures - 1))
        return float(min(delay, self._max_backoff))

    def status(self) -> dict[str, dict[str, Any]]:
        return {name: status.as_dict() for name, status in self._status.items()}

    def _launch(self, spec: _TaskSpec) -> None:
        self._tasks[spec.name] = asyncio.create_task(
            self._supervise(spec), name=f"supervised:{spec.name}"
        )

    async def _supervise(self, spec: _TaskSpec) -> None:
        status = self._status[spec.name]
        crash_times: deque[float] = deque()
        while True:
            started_at = self._clock()
            status.running = True
            error: Exception | None = None
            try:
                await spec.factory()
            except asyncio.CancelledError:
                status.running = False
                raise
            except Exception as exc:
                error = exc
            status.running = False

            now = self._clock()
            if now - started_at >= self._healthy_after:
                status.consecutive_failures = 0
            status.consecutive_failures += 1
            status.restarts += 1

            if error is None:
                logger.warning("Supervised task %s exited unexpectedly; restarting", spec.name)
            else:
                status.last_error = f"{type(error).__name__}: {error}"
                logger.error("Supervised task %s crashed", spec.name, exc_info=error)
                crash_times.append(now)
                while crash_times and now - crash_times[0] > self._crash_window:
                    crash_times.popleft()
                if len(crash_times) >= self._crash_threshold:
                    count = len(crash_times)
                    crash_times.clear()
                    await self._report_repeated_crash(spec.name, count, error)

            delay = self.backoff_for(status.consecutive_failures)
            logger.info("Restarting supervised task %s in %.1f s", spec.name, delay)
            await self._sleep(delay)

    async def _report_repeated_crash(self, name: str, count: int, error: Exception) -> None:
        logger.error(
            "Supervised task %s crashed %d times within %.0f s", name, count, self._crash_window
        )
        if self._on_repeated_crash is None:
            return
        try:
            result = self._on_repeated_crash(name, count, error)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Crash reporter failed for task %s", name)
