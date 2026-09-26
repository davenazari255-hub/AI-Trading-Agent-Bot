import asyncio

import pytest

from mooo_worker.supervisor import TaskSupervisor


async def test_restarts_with_exponential_backoff_and_reports() -> None:
    delays: list[float] = []
    reports: list[tuple[str, int]] = []
    attempts = 0
    healthy = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise RuntimeError("boom")
        healthy.set()
        await asyncio.Event().wait()

    async def reporter(name: str, exc: BaseException, failures: int) -> None:
        reports.append((name, failures))

    supervisor = TaskSupervisor(
        base_backoff_s=1,
        max_backoff_s=60,
        repeated_crash_threshold=3,
        on_repeated_crash=reporter,
        sleep=fake_sleep,
        clock=lambda: 0.0,
    )
    supervisor.add("flaky", flaky)
    runner = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(healthy.wait(), timeout=2)

    assert delays == [1, 2, 4]
    assert reports == [("flaky", 3)]
    assert supervisor.crash_counts["flaky"] == 3

    supervisor.request_stop()
    await asyncio.wait_for(runner, timeout=2)


async def test_failing_task_does_not_stop_other_tasks() -> None:
    ticks = 0
    steady_ran = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        await asyncio.sleep(0)

    async def always_fails() -> None:
        raise RuntimeError("down")

    async def steady() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            if ticks >= 3:
                steady_ran.set()
            await asyncio.sleep(0)

    supervisor = TaskSupervisor(sleep=fake_sleep, clock=lambda: 0.0)
    supervisor.add("fails", always_fails)
    supervisor.add("steady", steady)
    runner = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(steady_ran.wait(), timeout=2)
    supervisor.request_stop()
    await asyncio.wait_for(runner, timeout=2)

    assert supervisor.crash_counts["fails"] >= 1


def test_backoff_is_capped() -> None:
    supervisor = TaskSupervisor(base_backoff_s=1, max_backoff_s=60)
    assert supervisor.backoff_delay(1) == 1
    assert supervisor.backoff_delay(2) == 2
    assert supervisor.backoff_delay(7) == 60
    assert supervisor.backoff_delay(10_000) == 60


def test_duplicate_task_names_are_rejected() -> None:
    supervisor = TaskSupervisor()

    async def task() -> None:
        return None

    supervisor.add("a", task)
    with pytest.raises(ValueError):
        supervisor.add("a", task)
