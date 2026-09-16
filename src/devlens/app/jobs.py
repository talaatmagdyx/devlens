"""Asynchronous job execution and the run store.

Jobs and runs are different things and are stored separately: a job is a unit of
scheduling, a run is an investigation with a workflow version, a capability
snapshot and a tool-call log. Keeping them apart is what makes a report
explainable weeks later.

Execution goes through :class:`JobExecutor`. Today there is exactly one
implementation — asyncio tasks in this process — which is the right choice for a
single-node tool. The seam exists so that swapping it later is a new class
rather than a refactor of everything that touches jobs.

Interrupted runs are recovered at startup. A process that dies mid-run leaves
rows marked ``running``; without recovery the UI polls them forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from devlens.app.store import Database
from devlens.domain import (
    AccessDenied,
    AgentEvent,
    AnalysisRequest,
    AnalyzeRequest,
    CapacityReached,
    Conflict,
    ImplementRequest,
    JobCreate,
    JobRecord,
    PlatformRequest,
    ProviderError,
    ReviewRequest,
    RunRecord,
    ToolCall,
    digest,
)
from devlens.runtime import RunContext

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    command TEXT NOT NULL,
    project TEXT NOT NULL,
    status TEXT NOT NULL,
    progress TEXT,
    request_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    error_kind TEXT,
    idempotency_key TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_idempotency
    ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, created_at DESC);
"""

LEGACY_JOB_COLUMNS = (
    ("run_id", "TEXT"),
    ("error_kind", "TEXT"),
    ("idempotency_key", "TEXT"),
)


def _migrate_jobs_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if columns:
        for name, sql_type in LEGACY_JOB_COLUMNS:
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if event_columns and "seq" not in event_columns:
        conn.execute(
            """
            CREATE TABLE events_new (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                run_id TEXT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                data_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO events_new (job_id, run_id, ts, kind, message, data_json)
            SELECT job_id, NULL, ts, kind, message, data_json FROM events
            """
        )
        conn.execute("DROP TABLE events")
        conn.execute("ALTER TABLE events_new RENAME TO events")


RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    command TEXT NOT NULL,
    project TEXT NOT NULL,
    status TEXT NOT NULL,
    devlens_version TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    config_digest TEXT,
    model_provider TEXT,
    model_name TEXT,
    repository TEXT,
    commit_sha TEXT,
    requested_ref TEXT,
    ticket_key TEXT,
    ticket_snapshot_at REAL,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_runs_job ON runs(job_id);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    run_id TEXT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_job ON events(job_id, seq);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    error TEXT,
    result_digest TEXT,
    result_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tool_calls_run ON tool_calls(run_id, started_at);
"""

SCHEMA = SCHEMA + RUNS_SCHEMA

MAX_RESULT_BYTES = 4 * 1024 * 1024


def jobs_db_path() -> Path:
    raw = os.environ.get("DEVLENS_JOBS_DB")
    return Path(raw) if raw else Path.cwd() / ".devlens-jobs.sqlite"


def analyze_root() -> Path:
    """The directory local analysis is confined to.

    Defaults to the working directory rather than to "anywhere on this host".
    ``DEVLENS_ANALYZE_ROOT=/`` is an explicit, auditable opt-out.
    """
    raw = os.environ.get("DEVLENS_ANALYZE_ROOT", "").strip()
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def authorize_analyze_path(path: str) -> Path:
    """Confine a local path to the analyze root, resolving symlinks first."""
    target = Path(path).expanduser().resolve()
    base = analyze_root()
    if base != Path("/") and not target.is_relative_to(base):
        raise AccessDenied(
            f"Analyze path is outside the configured workspace root ({base}). "
            "Set DEVLENS_ANALYZE_ROOT to widen it."
        )
    return target


def request_payload(job: JobCreate):
    if job.command == "ticket":
        if not job.ticket_key or not job.repository:
            raise ProviderError("Ticket jobs require ticket_key and repository.")
        return AnalysisRequest(
            project=job.project,
            ticket_key=job.ticket_key,
            repository=job.repository,
            ref=job.ref,
        )
    if job.command == "analyze":
        if not job.path:
            raise ProviderError("Analyze jobs require a path.")
        authorize_analyze_path(job.path)
        return AnalyzeRequest(path=job.path, query=job.query)
    if job.command == "review":
        if not job.repository or job.pr is None:
            raise ProviderError("Review jobs require repository and pr.")
        return ReviewRequest(
            project=job.project,
            repository=job.repository,
            pr=job.pr,
            ticket_key=job.ticket_key,
            run_tests=job.run_tests,
        )
    if job.command == "implement":
        if not job.ticket_key or not job.repository:
            raise ProviderError("Implement jobs require ticket_key and repository.")
        return ImplementRequest(
            project=job.project,
            ticket_key=job.ticket_key,
            repository=job.repository,
            ref=job.ref,
            run_tests=job.run_tests,
        )
    if job.command == "ask" and not job.question and not job.path:
        raise ProviderError("Ask jobs require a question or a path.")
    if job.path:
        authorize_analyze_path(job.path)
    return PlatformRequest(
        project=job.project,
        question=job.question or job.query,
        path=job.path,
        ticket_key=job.ticket_key,
        repository=job.repository,
        ref=job.ref,
        pr=job.pr,
    )


class Notifier:
    """Wakes SSE streams the moment an event lands, instead of polling."""

    def __init__(self) -> None:
        self._waiters: dict[str, list[asyncio.Event]] = {}

    def subscribe(self, job_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._waiters.setdefault(job_id, []).append(event)
        return event

    def unsubscribe(self, job_id: str, event: asyncio.Event) -> None:
        waiters = self._waiters.get(job_id)
        if not waiters:
            return
        if event in waiters:
            waiters.remove(event)
        if not waiters:
            self._waiters.pop(job_id, None)

    def publish(self, job_id: str) -> None:
        for event in self._waiters.get(job_id, ()):
            event.set()


DEFAULT_HISTORY_LIMIT = 5_000


def history_limit_from_env() -> int:
    """How many finished jobs to keep, from ``DEVLENS_JOB_HISTORY``.

    Zero or a negative value keeps everything, which is a choice an operator can
    make deliberately — and now has to, rather than getting it by default.
    """
    raw = os.environ.get("DEVLENS_JOB_HISTORY", "").strip()
    if not raw:
        return DEFAULT_HISTORY_LIMIT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_HISTORY_LIMIT


class JobStore:
    """Durable jobs, runs, events and tool calls."""

    def __init__(self, path: Path | None = None, history_limit: int | None = None):
        self.path = path or jobs_db_path()
        self.db = Database(self.path, SCHEMA, migrate=_migrate_jobs_schema)
        self.notifier = Notifier()
        # How many finished jobs to keep. A soak found the reason this needs a
        # value at all: `prune` existed and nothing called it, so 270,000 runs
        # took the database to 1.5 GB. History is useful; unbounded history is
        # an outage on somebody's root volume.
        self.history_limit = (
            history_limit if history_limit is not None else history_limit_from_env()
        )
        self._since_prune = 0

    # -- lifecycle --------------------------------------------------------- #

    def recover_interrupted(self) -> int:
        """Fail runs that were in flight when the process died.

        Called at startup by every entry point. Without it a crash leaves rows
        marked ``running`` that nothing will ever complete.
        """
        now = time.time()
        with self.db.write() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status='failed', progress='interrupted', "
                "error='DevLens restarted while this run was in flight. Submit it again.', "
                "error_kind='interrupted', finished_at=? "
                "WHERE status IN ('queued','running')",
                (now,),
            )
            conn.execute(
                "UPDATE runs SET status='failed', finished_at=? "
                "WHERE status IN ('queued','running')",
                (now,),
            )
            return cursor.rowcount

    # -- jobs -------------------------------------------------------------- #

    def create(self, job: JobCreate, run_id: str) -> JobRecord:
        record = JobRecord(
            id=str(uuid4()),
            run_id=run_id,
            command=job.command,
            project=job.project,
            status="queued",
            progress="queued",
            request=job.model_dump(exclude={"idempotency_key"}),
            created_at=time.time(),
        )
        with self.db.write() as conn:
            if job.idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?",
                    (job.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return _job(existing)
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.run_id,
                    record.command,
                    record.project,
                    record.status,
                    record.progress,
                    json.dumps(record.request),
                    None,
                    None,
                    None,
                    job.idempotency_key,
                    record.created_at,
                    None,
                ),
            )
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def list(self, limit: int = 50, command: str | None = None) -> list[JobRecord]:
        limit = max(1, min(limit, 200))
        with self.db.connect() as conn:
            if command:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE command = ? ORDER BY created_at DESC LIMIT ?",
                    (command, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_job(row) for row in rows]

    def update(self, job_id: str, **fields) -> JobRecord | None:
        allowed = {"status", "progress", "result", "error", "error_kind", "finished_at"}
        assignments, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            column = "result_json" if key == "result" else key
            assignments.append(f"{column} = ?")
            values.append(_encode_result(value) if key == "result" else value)
        if not assignments:
            return self.get(job_id)
        values.append(job_id)
        with self.db.write() as conn:
            # Every fragment in `assignments` was built from the `allowed` set
            # above, so the only interpolated text is a literal column name;
            # values always travel as bound parameters.
            conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",  # noqa: S608
                values,
            )
        record = self.get(job_id)
        self.notifier.publish(job_id)
        if fields.get("status") in {"completed", "failed", "cancelled"}:
            self._maybe_prune()
        return record

    def _maybe_prune(self) -> None:
        """Bound the history, without paying for a DELETE on every single run.

        Pruning every hundredth finished job keeps the table within a hundred
        rows of the limit while leaving the hot path alone. A failure here is
        never allowed to fail the run that triggered it: the history growing is
        a problem for later, the operator's answer is a problem for now.
        """
        if self.history_limit <= 0:
            return
        self._since_prune += 1
        if self._since_prune < 100:
            return
        self._since_prune = 0
        with contextlib.suppress(sqlite3.Error):
            self.prune()

    # -- runs -------------------------------------------------------------- #

    def start_run(self, run: RunRecord) -> RunRecord:
        with self.db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.job_id,
                    run.command,
                    run.project,
                    run.status,
                    run.devlens_version,
                    run.workflow_version,
                    json.dumps(run.capabilities),
                    run.config_digest,
                    run.model_provider,
                    run.model_name,
                    run.repository,
                    run.commit,
                    run.requested_ref,
                    run.ticket_key,
                    run.ticket_snapshot_at,
                    run.started_at,
                    run.finished_at,
                ),
            )
        return run

    def finish_run(self, run_id: str, status: str, **fields) -> None:
        columns = {
            "repository": "repository",
            "commit": "commit_sha",
            "requested_ref": "requested_ref",
            "ticket_key": "ticket_key",
            "ticket_snapshot_at": "ticket_snapshot_at",
        }
        assignments = ["status = ?", "finished_at = ?"]
        values: list[Any] = [status, time.time()]
        for key, column in columns.items():
            if key in fields and fields[key] is not None:
                assignments.append(f"{column} = ?")
                values.append(fields[key])
        values.append(run_id)
        with self.db.write() as conn:
            # As above: column names come from `columns`, values are bound.
            conn.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE id = ?",  # noqa: S608
                values,
            )

    def run(self, run_id: str) -> RunRecord | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            calls = conn.execute(
                "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY started_at", (run_id,)
            ).fetchall()
        return RunRecord(
            id=row["id"],
            job_id=row["job_id"],
            command=row["command"],
            project=row["project"],
            status=row["status"],
            devlens_version=row["devlens_version"],
            workflow_version=row["workflow_version"],
            capabilities=json.loads(row["capabilities_json"]),
            config_digest=row["config_digest"],
            model_provider=row["model_provider"],
            model_name=row["model_name"],
            repository=row["repository"],
            commit=row["commit_sha"],
            requested_ref=row["requested_ref"],
            ticket_key=row["ticket_key"],
            ticket_snapshot_at=row["ticket_snapshot_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            tool_calls=[_tool_call(item) for item in calls],
        )

    # -- events and tool calls --------------------------------------------- #

    def add_event(self, job_id: str, event: AgentEvent, run_id: str | None = None) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO events (job_id, run_id, ts, kind, message, data_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    job_id,
                    run_id,
                    event.ts,
                    event.kind,
                    event.message,
                    json.dumps(event.data, default=str),
                ),
            )
        self.notifier.publish(job_id)

    def events(self, job_id: str, after: int = 0) -> Sequence[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE job_id = ? AND seq > ? ORDER BY seq LIMIT 500",
                (job_id, after),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "ts": row["ts"],
                "kind": row["kind"],
                "message": row["message"],
                "data": json.loads(row["data_json"] or "{}"),
            }
            for row in rows
        ]

    def record_tool_call(self, call: ToolCall) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tool_calls VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    call.id,
                    call.run_id,
                    call.tool,
                    json.dumps(call.arguments, default=str),
                    call.started_at,
                    call.finished_at,
                    call.status,
                    call.error,
                    call.result_digest,
                    call.result_bytes,
                ),
            )

    def prune(self, keep: int | None = None) -> int:
        """Drop the oldest finished jobs and their events. Bounded history.

        ``keep`` defaults to this store's configured history limit rather than
        a literal, so there is one number an operator can change and it is the
        one this actually uses.
        """
        keep = self.history_limit if keep is None else keep
        if keep <= 0:
            return 0
        with self.db.write() as conn:
            conn.execute(
                "DELETE FROM events WHERE job_id IN ("
                "  SELECT id FROM jobs WHERE status NOT IN ('queued','running')"
                "  ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (keep,),
            )
            cursor = conn.execute(
                "DELETE FROM jobs WHERE id IN ("
                "  SELECT id FROM jobs WHERE status NOT IN ('queued','running')"
                "  ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (keep,),
            )
            # Runs and their tool calls are the provenance trail, and they
            # outnumber jobs: bounding jobs alone left `runs` growing without
            # limit, which a soak caught as a database that kept climbing while
            # the job table sat still. Same limit, same rule about in-flight
            # rows, and tool calls go with the run they belong to.
            conn.execute(
                "DELETE FROM tool_calls WHERE run_id IN ("
                "  SELECT id FROM runs WHERE status NOT IN ('queued','running')"
                "  ORDER BY started_at DESC LIMIT -1 OFFSET ?)",
                (keep,),
            )
            conn.execute(
                "DELETE FROM runs WHERE id IN ("
                "  SELECT id FROM runs WHERE status NOT IN ('queued','running')"
                "  ORDER BY started_at DESC LIMIT -1 OFFSET ?)",
                (keep,),
            )
            return cursor.rowcount


class StoreRecorder:
    """Writes run events and tool calls straight through to the store."""

    def __init__(self, store: JobStore, job_id: str):
        self.store = store
        self.job_id = job_id

    def event(self, run_id: str, event: AgentEvent) -> None:
        self.store.add_event(self.job_id, event, run_id)

    def tool_call(self, call: ToolCall) -> None:
        self.store.record_tool_call(call)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


class JobExecutor(Protocol):
    """How a job body is run. One implementation today, by design."""

    def submit(self, job_id: str, coro) -> None: ...
    def cancel(self, job_id: str) -> bool: ...
    async def drain(self) -> None: ...
    @property
    def inflight(self) -> int: ...


class InProcessExecutor:
    """asyncio tasks in the API process.

    Correct for a single-node operator tool: no broker to run, no serialization
    boundary, and cancellation is immediate. The ceiling is one worker process,
    which is enforced rather than assumed.
    """

    def __init__(self, capacity: int = 32):
        self.capacity = capacity
        self.tasks: dict[str, asyncio.Task] = {}

    @property
    def inflight(self) -> int:
        return len(self.tasks)

    def submit(self, job_id: str, coro) -> None:
        if len(self.tasks) >= self.capacity:
            coro.close()
            raise CapacityReached(
                f"{self.capacity} jobs are already running; retry shortly."
            )
        task = asyncio.create_task(coro, name=f"devlens-job-{job_id}")
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    def cancel(self, job_id: str) -> bool:
        task = self.tasks.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    async def drain(self) -> None:
        pending = list(self.tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()


class JobService:
    """Submits, tracks and cancels jobs; owns the run lifecycle."""

    def __init__(
        self,
        store: JobStore,
        timeout: float = 300.0,
        executor: JobExecutor | None = None,
        guard=None,
        config_digest: str | None = None,
    ):
        self.store = store
        self.timeout = timeout
        self.executor = executor or InProcessExecutor()
        self.guard = guard
        self.config_digest = config_digest

    @property
    def tasks(self):  # pragma: no cover - compatibility shim for the API layer
        return getattr(self.executor, "tasks", {})

    def submit(self, agent, job: JobCreate) -> JobRecord:
        run_id = str(uuid4())
        record = self.store.create(job, run_id)
        if record.status != "queued" or record.run_id != run_id:
            return record  # idempotent replay of an earlier submission
        capabilities = sorted(self.guard.enabled) if self.guard else []
        model = (
            (self.guard.llm.kind, self.guard.llm.model)
            if self.guard and self.guard.llm
            else (None, None)
        )
        self.store.start_run(
            RunRecord(
                id=run_id,
                job_id=record.id,
                command=job.command,
                project=job.project,
                status="queued",
                capabilities=capabilities,
                config_digest=self.config_digest,
                model_provider=model[0],
                model_name=model[1],
                repository=job.repository,
                requested_ref=job.ref,
                ticket_key=job.ticket_key,
                started_at=record.created_at,
            )
        )
        self.executor.submit(record.id, self._run(agent, record.id, run_id, job))
        return record

    async def wait(self, job_id: str) -> JobRecord | None:
        task = getattr(self.executor, "tasks", {}).get(job_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self.store.get(job_id)

    async def close(self) -> None:
        await self.executor.drain()

    def cancel(self, job_id: str) -> JobRecord | None:
        record = self.store.get(job_id)
        if record is None:
            return None
        if record.status not in {"queued", "running"}:
            raise Conflict(f"Job is {record.status} and can no longer be cancelled.")
        self.executor.cancel(job_id)
        return self.store.update(
            job_id,
            status="cancelled",
            progress="cancelled",
            error="Cancelled by the operator.",
            error_kind="cancelled",
            finished_at=time.time(),
        )

    async def _run(self, agent, job_id: str, run_id: str, job: JobCreate) -> None:
        context = RunContext(
            run_id=run_id,
            recorder=StoreRecorder(self.store, job_id),
            capabilities=sorted(self.guard.enabled) if self.guard else [],
            model=(
                (self.guard.llm.kind, self.guard.llm.model)
                if self.guard and self.guard.llm
                else (None, None)
            ),
        )
        try:
            async with asyncio.timeout(self.timeout):
                await self._execute(agent, job_id, run_id, job, context)
        except TimeoutError:
            context.event("RunFailed", "exceeded its time budget")
            self.store.update(
                job_id,
                status="failed",
                progress="failed",
                error=f"Run exceeded its {self.timeout:g}s time budget.",
                error_kind="timeout",
                finished_at=time.time(),
            )
            self.store.finish_run(run_id, "failed")

    async def _execute(
        self, agent, job_id: str, run_id: str, job: JobCreate, context: RunContext
    ) -> None:
        self.store.update(job_id, status="running", progress=f"running {job.command}")
        try:
            payload = request_payload(job)
            context.event("RunStarted", f"{job.command} started", command=job.command)
            result = await _dispatch(agent, job.command, payload, context)
            dumped = result.model_dump() if hasattr(result, "model_dump") else result
            if not isinstance(dumped, dict):
                dumped = {"result": dumped}
            dumped["run_id"] = run_id
            context.completed()
            self.store.update(
                job_id,
                status="completed",
                progress="completed",
                result=dumped,
                finished_at=time.time(),
            )
            self.store.finish_run(
                run_id,
                "completed",
                repository=dumped.get("repository") or job.repository,
                commit=dumped.get("commit"),
                requested_ref=job.ref,
                ticket_key=job.ticket_key,
                ticket_snapshot_at=(dumped.get("ticket") or {}).get("snapshot_at")
                if isinstance(dumped.get("ticket"), dict)
                else None,
            )
        except asyncio.CancelledError:
            self.store.update(
                job_id,
                status="cancelled",
                progress="cancelled",
                error="Cancelled by the operator.",
                error_kind="cancelled",
                finished_at=time.time(),
            )
            self.store.finish_run(run_id, "cancelled")
            raise
        except Exception as exc:
            kind, message = classify(exc)
            context.event("RunFailed", message)
            self.store.update(
                job_id,
                status="failed",
                progress="failed",
                error=message,
                error_kind=kind,
                finished_at=time.time(),
            )
            self.store.finish_run(run_id, "failed")


async def _dispatch(agent, command: str, payload, context: RunContext):
    if command in {"analyze", "ask", "investigate", "design", "observe"}:
        if agent is not None and hasattr(agent, command):
            return await getattr(agent, command)(payload, context=context)
        return await _standalone(command, payload, context)
    if agent is None:
        raise ProviderError(
            "DevLens is not configured for this command; set the Jira and git "
            "environment variables described in the README."
        )
    return await getattr(agent, {"ticket": "analyze_ticket"}.get(command, command))(
        payload, context=context
    )


async def _standalone(command: str, payload, context: RunContext):
    """Run the commands that need no remote credentials, without an agent."""
    from devlens.agent.workflows.analyze import AnalyzeWorkflow
    from devlens.agent.workflows.platform import (
        AskWorkflow,
        DesignWorkflow,
        InvestigateWorkflow,
        ObserveWorkflow,
    )
    from devlens.guardrails import CapabilityGuard

    if command == "analyze":
        return await AnalyzeWorkflow().run(payload, context=context)
    guard = CapabilityGuard()
    workflows: dict[str, Any] = {
        "ask": AskWorkflow,
        "investigate": InvestigateWorkflow,
        "design": DesignWorkflow,
        "observe": ObserveWorkflow,
    }
    return await workflows[command](guard).run(payload, context=context)


#: Error taxonomy. Everything DevLens can fail at maps to a named kind with an
#: HTTP status, rather than collapsing into 500.
ERROR_KINDS: dict[str, tuple[str, int]] = {
    "AccessDenied": ("policy_denied", 403),
    "Conflict": ("conflict", 409),
    "CapacityReached": ("capacity", 429),
    "ProviderError": ("provider_error", 502),
    "CircuitOpenError": ("provider_unavailable", 503),
    "TimeoutError": ("timeout", 504),
    "ValidationError": ("invalid_input", 422),
    "ValueError": ("invalid_input", 422),
    "FileNotFoundError": ("not_found", 404),
    "OSError": ("io_error", 500),
    "StorageUnavailable": ("storage_unavailable", 503),
}


def classify(exc: BaseException) -> tuple[str, str]:
    """Map an exception to (kind, safe message)."""
    name = type(exc).__name__
    kind, _ = ERROR_KINDS.get(name, ("internal_error", 500))
    if kind == "internal_error":
        return kind, f"Internal error ({name})."
    return kind, str(exc)[:1000]


def status_for(exc: BaseException) -> int:
    return ERROR_KINDS.get(type(exc).__name__, ("internal_error", 500))[1]


def _encode_result(value: Any) -> str:
    encoded = json.dumps(value, default=str)
    if len(encoded) > MAX_RESULT_BYTES:
        trimmed = dict(value) if isinstance(value, dict) else {"result": value}
        trimmed["diff"] = None
        trimmed["truncated_result"] = True
        encoded = json.dumps(trimmed, default=str)[:MAX_RESULT_BYTES]
    return encoded


def _job(row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        run_id=row["run_id"],
        command=row["command"],
        project=row["project"],
        status=row["status"],
        progress=row["progress"],
        request=json.loads(row["request_json"] or "{}"),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=row["error"],
        error_kind=row["error_kind"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def _tool_call(row) -> ToolCall:
    return ToolCall(
        id=row["id"],
        run_id=row["run_id"],
        tool=row["tool"],
        arguments=json.loads(row["arguments_json"] or "{}"),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        error=row["error"],
        result_digest=row["result_digest"],
        result_bytes=row["result_bytes"],
    )


def config_fingerprint(settings) -> str:
    """A digest of the configuration a run executed under, without secrets."""
    return digest(
        {
            "projects": {
                name: {
                    "git_provider": project.git_provider,
                    "repositories": sorted(project.repositories),
                    "jira_projects": sorted(project.jira_projects),
                }
                for name, project in settings.projects.items()
            },
            "concurrency": settings.concurrency,
            "analysis_timeout": settings.analysis_timeout,
        }
    )
