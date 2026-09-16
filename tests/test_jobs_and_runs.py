"""Jobs, runs, provenance and the error taxonomy.

Regression coverage for DL-P1-007 (runs interrupted by a restart stayed
``running`` forever), DL-P1-008 (no capacity ceiling), DL-P1-012 (every failure
collapsed into a 500) and DL-P2-014 (local analysis was not confined to a root).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from devlens.app.jobs import (
    DEFAULT_HISTORY_LIMIT,
    ERROR_KINDS,
    InProcessExecutor,
    JobCreate,
    JobService,
    JobStore,
    analyze_root,
    authorize_analyze_path,
    classify,
    request_payload,
    status_for,
)
from devlens.domain import (
    AccessDenied,
    AgentEvent,
    CapacityReached,
    Conflict,
    ProviderError,
    RunRecord,
)
from devlens.guardrails import CapabilityGuard
from devlens.runtime import RunContext


@pytest.fixture
def store(jobs_db) -> JobStore:
    return JobStore(jobs_db)


class StubAgent:
    """An agent whose commands are controlled by the test."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or (lambda payload: {"ok": True})
        self.seen: list = []

    async def ask(self, payload, context=None):
        self.seen.append(payload)
        async with context.tool("stub.ask", question=payload.question) as box:
            box["result"] = "answer"
        outcome = self.behaviour(payload)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    analyze = ask
    investigate = ask
    observe = ask
    design = ask


def ask_job(**overrides) -> JobCreate:
    return JobCreate(command="ask", question="why is checkout slow?", **overrides)


def test_legacy_job_database_is_upgraded(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                command TEXT,
                project TEXT,
                status TEXT,
                progress TEXT,
                request_json TEXT,
                result_json TEXT,
                error TEXT,
                created_at REAL,
                finished_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE events (
                job_id TEXT,
                ts REAL,
                kind TEXT,
                message TEXT,
                data_json TEXT
            )
            """
        )
    store = JobStore(path)
    record = store.create(ask_job(idempotency_key="legacy-idem-1"), run_id="run-legacy")
    assert record.id
    store.add_event(record.id, AgentEvent(ts=time.time(), kind="info", message="migrated"))
    assert store.events(record.id)


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


def test_a_restart_fails_runs_that_were_in_flight(store, jobs_db):
    """DL-P1-007: without this, the UI polls a dead run forever."""
    record = store.create(ask_job(), run_id="run-1")
    store.update(record.id, status="running")
    store.start_run(
        RunRecord(
            id="run-1",
            job_id=record.id,
            command="ask",
            project="default",
            status="running",
            started_at=time.time(),
        )
    )
    store.db.close()

    reopened = JobStore(jobs_db)
    assert reopened.recover_interrupted() == 1
    recovered = reopened.get(record.id)
    assert recovered.status == "failed"
    assert recovered.error_kind == "interrupted"
    assert "restarted" in recovered.error
    assert reopened.run("run-1").status == "failed"


def test_recovery_leaves_finished_jobs_alone(store):
    record = store.create(ask_job(), run_id="run-1")
    store.update(record.id, status="completed", result={"ok": True})
    assert store.recover_interrupted() == 0
    assert store.get(record.id).status == "completed"


# --------------------------------------------------------------------------- #
# Idempotency and capacity
# --------------------------------------------------------------------------- #


def test_the_same_idempotency_key_returns_the_same_job(store):
    first = store.create(ask_job(idempotency_key="idem-abc-1"), run_id="run-1")
    second = store.create(ask_job(idempotency_key="idem-abc-1"), run_id="run-2")
    assert second.id == first.id
    assert second.run_id == "run-1"
    assert len(store.list()) == 1


@pytest.mark.asyncio
async def test_a_replayed_submission_does_not_start_a_second_run(store):
    service = JobService(store, executor=InProcessExecutor(capacity=4))
    agent = StubAgent()
    first = service.submit(agent, ask_job(idempotency_key="idem-key-1"))
    second = service.submit(agent, ask_job(idempotency_key="idem-key-1"))
    await service.wait(first.id)
    await service.close()
    assert first.id == second.id
    assert len(agent.seen) == 1


@pytest.mark.asyncio
async def test_capacity_is_enforced_rather_than_assumed(store):
    """DL-P1-008: the ceiling is a refusal with a retry hint, not memory growth."""
    service = JobService(store, executor=InProcessExecutor(capacity=1))
    gate = asyncio.Event()

    class Blocking(StubAgent):
        async def ask(self, payload, context=None):
            await gate.wait()
            return {"ok": True}

    agent = Blocking()
    service.submit(agent, ask_job())
    await asyncio.sleep(0)
    with pytest.raises(CapacityReached, match="retry shortly"):
        service.submit(agent, ask_job())
    gate.set()
    await service.close()


def test_a_refused_submission_does_not_leak_the_coroutine(store):
    executor = InProcessExecutor(capacity=0)

    async def body():  # pragma: no cover - must never be awaited
        raise AssertionError("this coroutine should have been closed")

    with pytest.raises(CapacityReached):
        executor.submit("job", body())
    assert executor.inflight == 0


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_completed_run_records_its_provenance(store):
    guard = CapabilityGuard({"llm"})
    service = JobService(store, guard=guard, config_digest="digest-1")
    record = service.submit(StubAgent(), ask_job())
    await service.wait(record.id)

    job = store.get(record.id)
    assert job.status == "completed"
    assert job.result["run_id"] == record.run_id

    run = store.run(record.run_id)
    assert run.status == "completed"
    assert run.capabilities == ["llm"]
    assert run.config_digest == "digest-1"
    assert [call.tool for call in run.tool_calls] == ["stub.ask"]
    assert run.tool_calls[0].status == "ok"


@pytest.mark.asyncio
async def test_a_failing_run_is_classified_not_collapsed_into_500(store):
    """DL-P1-012: the operator is told which of eight things went wrong."""
    service = JobService(store)
    record = service.submit(
        StubAgent(lambda payload: AccessDenied("Repository is not allowed.")), ask_job()
    )
    await service.wait(record.id)
    job = store.get(record.id)
    assert job.status == "failed"
    assert job.error_kind == "policy_denied"
    assert job.error == "Repository is not allowed."


@pytest.mark.asyncio
async def test_an_unexpected_exception_does_not_leak_its_message(store):
    service = JobService(store)
    record = service.submit(
        StubAgent(lambda payload: RuntimeError("secret=hunter2 at /srv/devlens")),
        ask_job(),
    )
    await service.wait(record.id)
    job = store.get(record.id)
    assert job.error_kind == "internal_error"
    assert "hunter2" not in job.error
    assert job.error == "Internal error (RuntimeError)."


@pytest.mark.asyncio
async def test_a_run_that_overruns_its_budget_is_failed_with_that_reason(store):
    service = JobService(store, timeout=0.05)

    class Slow(StubAgent):
        async def ask(self, payload, context=None):
            await asyncio.sleep(5)

    record = service.submit(Slow(), ask_job())
    await service.wait(record.id)
    job = store.get(record.id)
    assert job.status == "failed"
    assert job.error_kind == "timeout"
    assert "time budget" in job.error
    assert store.run(record.run_id).status == "failed"


@pytest.mark.asyncio
async def test_cancelling_a_running_job_records_the_cancellation(store):
    service = JobService(store)

    class Slow(StubAgent):
        async def ask(self, payload, context=None):
            await asyncio.sleep(5)

    record = service.submit(Slow(), ask_job())
    await asyncio.sleep(0)
    cancelled = service.cancel(record.id)
    assert cancelled.status == "cancelled"
    await service.close()


def test_cancelling_a_finished_job_is_a_conflict(store):
    service = JobService(store)
    record = store.create(ask_job(), run_id="run-1")
    store.update(record.id, status="completed")
    with pytest.raises(Conflict, match="can no longer be cancelled"):
        service.cancel(record.id)


def test_cancelling_an_unknown_job_returns_none(store):
    assert JobService(store).cancel("nope") is None


# --------------------------------------------------------------------------- #
# The error taxonomy itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("exc", "kind", "status"),
    [
        (AccessDenied("x"), "policy_denied", 403),
        (Conflict("x"), "conflict", 409),
        (CapacityReached("x"), "capacity", 429),
        (ProviderError("x"), "provider_error", 502),
        (TimeoutError(), "timeout", 504),
        (ValueError("x"), "invalid_input", 422),
        (FileNotFoundError("x"), "not_found", 404),
        (RuntimeError("x"), "internal_error", 500),
    ],
)
def test_every_failure_has_a_kind_and_a_status(exc, kind, status):
    assert classify(exc)[0] == kind
    assert status_for(exc) == status


def test_no_kind_maps_to_a_status_outside_the_taxonomy():
    assert all(400 <= status <= 599 for _, status in ERROR_KINDS.values())


# --------------------------------------------------------------------------- #
# Path confinement
# --------------------------------------------------------------------------- #


def test_analysis_is_confined_to_the_configured_root(tmp_path, monkeypatch):
    """DL-P2-014: the default is the working directory, not the whole host."""
    root = tmp_path / "workspace"
    (root / "inner").mkdir(parents=True)
    monkeypatch.setenv("DEVLENS_ANALYZE_ROOT", str(root))
    assert analyze_root() == root.resolve()
    assert authorize_analyze_path(str(root / "inner")) == (root / "inner").resolve()
    for escape in ("/etc", str(tmp_path), str(root / ".." / "elsewhere")):
        with pytest.raises(AccessDenied, match="outside the configured workspace root"):
            authorize_analyze_path(escape)


def test_a_symlink_out_of_the_root_is_refused(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "link").symlink_to(outside)
    monkeypatch.setenv("DEVLENS_ANALYZE_ROOT", str(root))
    with pytest.raises(AccessDenied):
        authorize_analyze_path(str(root / "link"))


def test_widening_the_root_to_the_whole_host_is_explicit(monkeypatch):
    monkeypatch.setenv("DEVLENS_ANALYZE_ROOT", "/")
    assert authorize_analyze_path("/etc") == __import__("pathlib").Path("/etc")


def test_the_default_root_is_the_working_directory(monkeypatch):
    monkeypatch.delenv("DEVLENS_ANALYZE_ROOT", raising=False)
    assert analyze_root() == __import__("pathlib").Path.cwd().resolve()


@pytest.mark.parametrize(
    ("job", "message"),
    [
        (JobCreate(command="ticket"), "ticket_key and repository"),
        (JobCreate(command="analyze"), "require a path"),
        (JobCreate(command="review", repository="org/repo"), "repository and pr"),
        (JobCreate(command="implement", repository="org/repo"), "ticket_key and repository"),
        (JobCreate(command="ask"), "question or a path"),
    ],
)
def test_an_incomplete_request_is_refused_before_anything_runs(job, message):
    with pytest.raises(ProviderError, match=message):
        request_payload(job)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def test_events_are_returned_after_a_cursor(store):
    record = store.create(ask_job(), run_id="run-1")
    for index in range(5):
        store.add_event(record.id, AgentEvent(ts=time.time(), kind="k", message=f"m{index}"))
    everything = store.events(record.id)
    assert [item["message"] for item in everything] == ["m0", "m1", "m2", "m3", "m4"]
    tail = store.events(record.id, after=everything[2]["seq"])
    assert [item["message"] for item in tail] == ["m3", "m4"]


@pytest.mark.asyncio
async def test_a_subscriber_is_woken_when_an_event_lands(store):
    record = store.create(ask_job(), run_id="run-1")
    waiter = store.notifier.subscribe(record.id)
    assert not waiter.is_set()
    store.add_event(record.id, AgentEvent(ts=time.time(), kind="k", message="m"))
    await asyncio.wait_for(waiter.wait(), timeout=1)
    store.notifier.unsubscribe(record.id, waiter)
    store.notifier.publish(record.id)  # no subscribers left; must not raise


def test_tool_call_arguments_are_redacted_before_they_are_stored(store):
    context = RunContext(run_id="run-1")

    async def call() -> None:
        async with context.tool("jira.fetch", token="ghp_" + "a" * 36, ticket_key="DEV-1"):
            pass

    asyncio.run(call())
    recorded = context.calls[0]
    assert recorded.arguments["token"] == "***"
    assert recorded.arguments["ticket_key"] == "DEV-1"


def test_a_failed_tool_call_is_recorded_as_denied(store):
    context = RunContext(run_id="run-1")

    async def call() -> None:
        async with context.tool("git.push"):
            raise AccessDenied("nope")

    with pytest.raises(AccessDenied):
        asyncio.run(call())
    assert context.calls[0].status == "denied"
    assert [event.kind for event in context.events][-1] == "ToolFailed"


# --------------------------------------------------------------------------- #
# Bounded history
# --------------------------------------------------------------------------- #


def test_history_is_pruned_to_a_ceiling(store):
    for index in range(12):
        record = store.create(ask_job(idempotency_key=f"idem-key-{index}"), run_id=f"run-{index}")
        store.add_event(record.id, AgentEvent(ts=time.time(), kind="k", message="m"))
        store.update(record.id, status="completed")
    assert store.prune(keep=5) == 7
    assert len(store.list(limit=200)) == 5


def test_an_oversized_result_is_trimmed_rather_than_rejected(store):
    record = store.create(ask_job(), run_id="run-1")
    store.update(record.id, status="completed", result={"diff": "x" * 5_000_000})
    stored = store.get(record.id)
    assert stored.result["truncated_result"] is True
    assert stored.result["diff"] is None


# --------------------------------------------------------------------------- #
# History is bounded
# --------------------------------------------------------------------------- #


def test_history_is_pruned_as_runs_finish(tmp_path):
    """The defect a 75-minute soak found: `prune` existed and nothing called it.

    270,000 runs took the job database to 1.5 GB. Nothing failed — it would
    simply have kept going until the volume filled.
    """
    store = JobStore(tmp_path / "jobs.sqlite", history_limit=50)
    for index in range(400):
        record = store.create(JobCreate(command="ask", question=f"q{index}"), f"run-{index}")
        store.update(record.id, status="completed", finished_at=1.0)

    with store.db.connect() as conn:
        rows = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    assert rows <= 150, f"history grew to {rows} rows with a limit of 50"
    kept = store.list()
    # The newest are the ones kept: an operator looking for the run they just
    # submitted must still find it.
    assert kept[0].request["question"] == "q399"


def test_a_run_still_in_flight_is_never_pruned(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite", history_limit=5)
    live = store.create(JobCreate(command="ask", question="in flight"), "run-live")
    for index in range(300):
        record = store.create(JobCreate(command="ask", question=f"q{index}"), f"run-{index}")
        store.update(record.id, status="completed", finished_at=1.0)
    assert store.get(live.id) is not None


def test_an_operator_can_keep_everything_deliberately(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVLENS_JOB_HISTORY", "0")
    store = JobStore(tmp_path / "jobs.sqlite")
    assert store.history_limit == 0
    for index in range(250):
        record = store.create(JobCreate(command="ask", question=f"q{index}"), f"run-{index}")
        store.update(record.id, status="completed", finished_at=1.0)
    with store.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 250


def test_an_unreadable_history_limit_falls_back_rather_than_crashing(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVLENS_JOB_HISTORY", "not a number")
    store = JobStore(tmp_path / "jobs.sqlite")
    assert store.history_limit == DEFAULT_HISTORY_LIMIT


def test_the_provenance_trail_is_bounded_too(tmp_path):
    """Bounding jobs alone is not bounding the database.

    The first fix pruned jobs and events; a soak then showed the file still
    climbing while the job table sat at its limit, because every run — the
    provenance record, one per job, plus its tool calls — was kept forever.
    """
    store = JobStore(tmp_path / "jobs.sqlite", history_limit=50)
    for index in range(400):
        record = store.create(JobCreate(command="ask", question=f"q{index}"), f"run-{index}")
        store.start_run(
            RunRecord(
                id=f"run-{index}",
                job_id=record.id,
                command="ask",
                project="default",
                status="completed",
                started_at=float(index),
            )
        )
        store.update(record.id, status="completed", finished_at=1.0)

    with store.db.connect() as conn:
        runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    assert runs <= 150, f"the run table grew to {runs} rows with a limit of 50"


def test_a_run_in_flight_survives_pruning(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite", history_limit=5)
    store.start_run(
        RunRecord(
            id="run-live",
            job_id=None,
            command="ask",
            project="default",
            status="running",
            started_at=0.0,
        )
    )
    for index in range(300):
        record = store.create(JobCreate(command="ask", question=f"q{index}"), f"run-{index}")
        store.start_run(
            RunRecord(
                id=f"run-{index}",
                job_id=record.id,
                command="ask",
                project="default",
                status="completed",
                started_at=float(index + 1),
            )
        )
        store.update(record.id, status="completed", finished_at=1.0)
    assert store.run("run-live") is not None
