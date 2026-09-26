import asyncio
import itertools

import pytest

from mooo_worker.supervisor import TaskSupervisor


class _SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


def _failing_then_idle(failures: int, reached: asyncio.Event) -> tuple[list[int], object]:
    calls: list[int] = []

    async def factory() -> None:
        calls.append(1)
        if len(calls) <= failures:
            raise RuntimeError("boom")
        reached.set()
        await asyncio.Event().wait()

    return calls, factory


async def test_restarts_with_exponential_backoff() -> None:
    sleep = _SleepRecorder()
    supervisor = TaskSupervisor(initial_backoff=1, max_backoff=60, sleep=sleep, clock=lambda: 0.0)
    reached = asyncio.Event()
    calls, factory = _failing_then_idle(4, reached)
    supervisor.add("flaky", factory)  # type: ignore[arg-type]
    supervisor.start()
    await asyncio.wait_for(reached.wait(), timeout=2)

    status = supervisor.status()["flaky"]
    assert sleep.delays == [1, 2, 4, 8]
    assert status["restarts"] == 4
    assert status["running"] is True
    assert status["last_error"] == "RuntimeError: boom"
    assert len(calls) == 5
    await supervisor.stop()
    assert supervisor.status()["flaky"]["running"] is False


def test_backoff_is_capped() -> None:
    supervisor = TaskSupervisor(initial_backoff=1, max_backoff=30)
    assert [supervisor.backoff_for(n) for n in range(0, 8)] == [0, 1, 2, 4, 8, 16, 30, 30]


async def test_backoff_resets_after_healthy_run() -> None:
    sleep = _SleepRecorder()
    ticks = itertools.count(0, 100)
    supervisor = TaskSupervisor(
        initial_backoff=1, healthy_after=60, sleep=sleep, clock=lambda: float(next(ticks))
    )
    reached = asyncio.Event()
    _calls, factory = _failing_then_idle(3, reached)
    supervisor.add("steady", factory)  # type: ignore[arg-type]
    supervisor.start()
    await asyncio.wait_for(reached.wait(), timeout=2)
    await supervisor.stop()
    assert sleep.delays == [1, 1, 1]


async def test_repeated_crashes_are_reported() -> None:
    reports: list[tuple[str, int, str]] = []

    async def reporter(name: str, count: int, error: BaseException) -> None:
        reports.append((name, count, str(error)))

    supervisor = TaskSupervisor(
        initial_backoff=1,
        crash_threshold=3,
        crash_window=300,
        on_repeated_crash=reporter,
        sleep=_SleepRecorder(),
        clock=lambda: 0.0,
    )
    reached = asyncio.Event()
    _calls, factory = _failing_then_idle(5, reached)
    supervisor.add("crashy", factory)  # type: ignore[arg-type]
    supervisor.start()
    await asyncio.wait_for(reached.wait(), timeout=2)
    await supervisor.stop()
    assert reports == [("crashy", 3, "boom")]


async def test_failing_reporter_does_not_stop_supervision() -> None:
    def reporter(_name: str, _count: int, _error: BaseException) -> None:
        raise RuntimeError("reporter down")

    supervisor = TaskSupervisor(
        crash_threshold=1, on_repeated_crash=reporter, sleep=_SleepRecorder(), clock=lambda: 0.0
    )
    reached = asyncio.Event()
    _calls, factory = _failing_then_idle(2, reached)
    supervisor.add("task", factory)  # type: ignore[arg-type]
    supervisor.start()
    await asyncio.wait_for(reached.wait(), timeout=2)
    await supervisor.stop()
    assert supervisor.status()["task"]["restarts"] == 2


async def test_normal_exit_is_restarted() -> None:
    sleep = _SleepRecorder()
    runs: list[int] = []
    reached = asyncio.Event()

    async def factory() -> None:
        runs.append(1)
        if len(runs) >= 3:
            reached.set()
            await asyncio.Event().wait()

    supervisor = TaskSupervisor(sleep=sleep, clock=lambda: 0.0)
    supervisor.add("returns", factory)
    supervisor.start()
    await asyncio.wait_for(reached.wait(), timeout=2)
    await supervisor.stop()
    assert len(runs) == 3
    assert supervisor.status()["returns"]["last_error"] is None


async def test_one_failing_task_does_not_stop_others() -> None:
    healthy_ran = asyncio.Event()

    async def healthy() -> None:
        healthy_ran.set()
        await asyncio.Event().wait()

    async def broken() -> None:
        raise RuntimeError("always")

    supervisor = TaskSupervisor(sleep=_SleepRecorder(), clock=lambda: 0.0)
    supervisor.add("healthy", healthy)
    supervisor.add("broken", broken)
    supervisor.start()
    await asyncio.wait_for(healthy_ran.wait(), timeout=2)
    for _ in range(20):
        await asyncio.sleep(0)
    assert supervisor.status()["healthy"]["running"] is True
    assert supervisor.status()["healthy"]["restarts"] == 0
    assert supervisor.status()["broken"]["restarts"] > 0
    await supervisor.stop()


async def test_stop_cancels_without_counting_a_restart() -> None:
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    supervisor = TaskSupervisor()
    supervisor.add("forever", forever)
    supervisor.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await supervisor.stop()
    status = supervisor.status()["forever"]
    assert status["restarts"] == 0
    assert status["running"] is False
    assert supervisor.running is False


async def test_task_added_after_start_is_launched() -> None:
    started = asyncio.Event()

    async def late() -> None:
        started.set()
        await asyncio.Event().wait()

    supervisor = TaskSupervisor()
    supervisor.start()
    supervisor.add("late", late)
    await asyncio.wait_for(started.wait(), timeout=2)
    await supervisor.stop()


def test_duplicate_task_names_are_rejected() -> None:
    async def noop() -> None:
        return None

    supervisor = TaskSupervisor()
    supervisor.add("a", noop)
    with pytest.raises(ValueError):
        supervisor.add("a", noop)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_backoff": 0},
        {"initial_backoff": 10, "max_backoff": 5},
        {"backoff_multiplier": 0.5},
        {"crash_threshold": 0},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        TaskSupervisor(**kwargs)  # type: ignore[arg-type]
