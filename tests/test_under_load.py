"""Behaviour under sustained load.

The audit's charge against this area was not that the limits were wrong but
that they had only ever been exercised one call at a time. Everything here runs
the real component with real concurrency: hundreds of simultaneous submissions,
thousands of writes, dozens of live event streams.

What is asserted is the property, never a timing that would make the suite
flaky on a loaded machine.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from devlens.agent.audit import AuditLog
from devlens.app.api import app
from devlens.app.approvals import ApprovalService
from devlens.app.auth import AuthGate
from devlens.app.jobs import InProcessExecutor, JobCreate, JobService, JobStore
from devlens.domain import AgentEvent, CapacityReached, ProviderError
from devlens.guardrails import CapabilityGuard
from devlens.resilience import CircuitBreaker, CircuitOpenError, RateLimiter

# --------------------------------------------------------------------------- #
# Capacity under a burst
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_hundred_submissions_never_exceed_the_ceiling(jobs_db):
    """The ceiling holds under a burst, and refusals are refusals, not crashes."""
    store = JobStore(jobs_db)
    service = JobService(store, executor=InProcessExecutor(capacity=8))
    release = asyncio.Event()
    peak = 0

    class Slow:
        async def ask(self, payload, context=None):
            nonlocal peak
            peak = max(peak, service.executor.inflight)
            await release.wait()
            return {"ok": True}

    agent = Slow()
    admitted, refused = 0, 0
    for index in range(200):
        try:
            service.submit(agent, JobCreate(command="ask", question=f"q{index}"))
            admitted += 1
        except CapacityReached:
            refused += 1
        if index % 20 == 0:
            await asyncio.sleep(0)

    assert admitted == 8, "the ceiling admitted more than its capacity"
    assert refused == 192
    assert peak <= 8
    release.set()
    await service.close()
    assert service.executor.inflight == 0, "a refused burst leaked a task"


@pytest.mark.asyncio
async def test_capacity_recovers_as_work_drains(jobs_db):
    store = JobStore(jobs_db)
    service = JobService(store, executor=InProcessExecutor(capacity=4))
    gate = asyncio.Event()

    class Slow:
        async def ask(self, payload, context=None):
            await gate.wait()

    agent = Slow()
    for index in range(4):
        service.submit(agent, JobCreate(command="ask", question=f"q{index}"))
    with pytest.raises(CapacityReached):
        service.submit(agent, JobCreate(command="ask", question="overflow"))

    gate.set()
    await asyncio.sleep(0.05)
    # Once the queue drains the ceiling is available again rather than stuck.
    service.submit(agent, JobCreate(command="ask", question="after"))
    gate.set()
    await service.close()


# --------------------------------------------------------------------------- #
# Rate limiting and the circuit, under concurrency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_rate_limiter_paces_a_thousand_concurrent_callers():
    limiter = RateLimiter(rate=500, burst=50)
    started = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(1000)))
    elapsed = time.monotonic() - started
    # 1000 permits, 50 free, the rest at 500/s: at least ~1.9s of pacing.
    assert elapsed >= 1.7, f"the limiter admitted 1000 calls in {elapsed:.2f}s"
    assert elapsed < 10, "pacing should not be quadratic in the number of waiters"


@pytest.mark.asyncio
async def test_a_failing_dependency_is_called_a_bounded_number_of_times():
    """500 callers hit a dead provider; the circuit stops the stampede."""
    breaker = CircuitBreaker(failure_threshold=5, recovery_seconds=30.0)
    attempts = 0

    async def failing():
        nonlocal attempts
        attempts += 1
        raise ProviderError("down")

    async def call():
        try:
            await breaker.call(failing)
        except (ProviderError, CircuitOpenError) as exc:
            return type(exc).__name__

    outcomes = await asyncio.gather(*(call() for _ in range(500)))
    assert attempts <= 20, f"the dead provider was called {attempts} times"
    assert outcomes.count("CircuitOpenError") > 400
    assert "ProviderError" in outcomes


@pytest.mark.asyncio
async def test_the_circuit_reopens_rather_than_latching_shut():
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=0.05)

    async def failing():
        raise ProviderError("down")

    async def working():
        return "ok"

    for _ in range(50):
        with pytest.raises((ProviderError, CircuitOpenError)):
            await breaker.call(failing)
    await asyncio.sleep(0.08)
    assert await breaker.call(working) == "ok"
    # And it keeps serving once healthy.
    results = await asyncio.gather(*(breaker.call(working) for _ in range(200)))
    assert results == ["ok"] * 200


# --------------------------------------------------------------------------- #
# The store under concurrent writers
# --------------------------------------------------------------------------- #


def test_sixteen_threads_writing_at_once_never_hit_a_locked_database(jobs_db):
    """WAL plus a real busy timeout, exercised rather than asserted."""
    store = JobStore(jobs_db)
    record = store.create(JobCreate(command="ask", question="why?"), run_id="run-1")
    errors: list[str] = []

    def writer(index: int) -> None:
        try:
            for step in range(50):
                store.add_event(
                    record.id,
                    AgentEvent(ts=time.time(), kind="Step", message=f"{index}-{step}"),
                )
        except sqlite3.OperationalError as exc:  # pragma: no cover - the failure case
            errors.append(str(exc))

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"concurrent writers failed: {errors[:3]}"
    assert len(store.events(record.id, after=0)) == 500, "the read is capped at 500 by design"
    with store.db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM events WHERE job_id = ?", (record.id,)
        ).fetchone()[0]
    assert total == 800, "every write landed"


def test_history_stays_bounded_over_a_long_soak(jobs_db):
    """2,000 runs through the store; history is pruned, not grown forever."""
    store = JobStore(jobs_db)
    for index in range(2_000):
        record = store.create(
            JobCreate(command="ask", question=f"q{index}", idempotency_key=f"soak-{index:05d}"),
            run_id=f"run-{index}",
        )
        store.add_event(record.id, AgentEvent(ts=time.time(), kind="Step", message="m"))
        store.update(record.id, status="completed", result={"ok": True})

    store.prune(keep=500)
    assert len(store.list(limit=200)) == 200
    with store.db.connect() as conn:
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert jobs == 500
    assert events == 500, "pruning a job must take its events with it"


def test_an_approval_queue_under_a_burst_admits_exactly_its_ceiling(jobs_db, monkeypatch):
    from tests.test_approvals import proposal

    monkeypatch.setattr("devlens.app.approvals.MAX_PENDING", 25)
    service = ApprovalService(jobs_db)
    stored = service.propose(proposal())
    admitted, refused = 0, 0
    for _ in range(200):
        try:
            service.request(stored.id)
            admitted += 1
        except ProviderError:
            refused += 1
    assert admitted == 25
    assert refused == 175


# --------------------------------------------------------------------------- #
# Many live event streams
# --------------------------------------------------------------------------- #


def test_forty_concurrent_event_streams_all_terminate_and_unsubscribe(
    jobs_db, monkeypatch, tmp_path
):
    from starlette.datastructures import State

    store = JobStore(jobs_db)
    app.state = State()
    app.state.auth = AuthGate(None)
    app.state.guard = CapabilityGuard()
    app.state.audit = AuditLog()
    app.state.jobs = JobService(store)
    app.state.approvals = ApprovalService(tmp_path / "approvals.sqlite")
    app.state.agent = None
    app.state.public_settings = None
    app.state.config_error = None
    app.state.hosted = False

    record = store.create(JobCreate(command="ask", question="why?"), run_id="run-1")
    for index in range(10):
        store.add_event(record.id, AgentEvent(ts=time.time(), kind="Step", message=f"m{index}"))
    store.update(record.id, status="completed")

    client = TestClient(app)
    bodies = []
    errors: list[str] = []

    def reader() -> None:
        try:
            bodies.append(client.get(f"/jobs/{record.id}/events").text)
        except Exception as exc:  # pragma: no cover - the failure case
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=reader) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(bodies) == 40
    assert all("RunFinished" in body for body in bodies)
    assert all(body.count("m9") == 1 for body in bodies)
    # Every subscriber released its slot; nothing is left waiting on the job.
    assert store.notifier._waiters.get(record.id) in (None, [])
