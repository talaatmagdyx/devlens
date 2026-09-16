"""HTTP API and operator UI host.

Security posture of this entry point, stated plainly so it is not mistaken for
something stronger: it is a **single-operator interface**. It has an attempt
quota, expiring rotating sessions, an origin check on every state-changing
request, security headers, a body-size ceiling and a confined analyze root. It
does not have identities, roles or per-user audit. Bind it to loopback.

Errors map through a named taxonomy rather than collapsing into 500, so a denied
policy is a 403, a stale approval is a 409, a full queue is a 429 and an
unreachable provider is a 502.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from devlens.agent.audit import AuditLog
from devlens.agent.intent import detect_intent
from devlens.agent.workflows.analyze import AnalyzeWorkflow
from devlens.app.approvals import ApprovalService
from devlens.app.auth import COOKIE, AuthGate
from devlens.app.catalog import (
    KnowledgeCreate,
    KnowledgeStore,
    OnboardingStore,
    capacity,
    notifications,
    search,
    services,
    settings_section,
)
from devlens.app.config import Settings
from devlens.app.dependencies import agent_context
from devlens.app.export import to_markdown
from devlens.app.jobs import (
    JobService,
    JobStore,
    analyze_root,
    authorize_analyze_path,
    classify,
    config_fingerprint,
    request_payload,
    status_for,
)
from devlens.app.persistence import (
    PersistentKnowledgeStore,
    PersistentOnboardingStore,
    SQLiteStateRepository,
)
from devlens.domain import (
    DEVLENS_VERSION,
    AccessDenied,
    AnalysisRequest,
    AnalysisResult,
    AnalyzeRequest,
    ApprovalCreate,
    CapacityReached,
    Conflict,
    EngineeringReport,
    ImplementRequest,
    JobCreate,
    JobRecord,
    LoginRequest,
    PlatformRequest,
    ProjectPublic,
    ProposalCreate,
    ProviderError,
    ReviewRequest,
)
from devlens.guardrails import CapabilityGuard
from devlens.providers.llm import auth_methods
from devlens.runtime import RunContext

MAX_BODY_BYTES = 1_048_576
SSE_IDLE_SECONDS = 15.0

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.auth = AuthGate.from_env()
    app.state.guard = CapabilityGuard.from_env()
    app.state.audit = AuditLog.from_env()
    store = JobStore()
    recovered = store.recover_interrupted()
    if recovered:
        app.state.audit.record({"event": "jobs.recovered", "count": recovered})
    state = SQLiteStateRepository(store.path.with_suffix(".state.sqlite"))
    app.state.knowledge = PersistentKnowledgeStore(state)
    app.state.onboarding = PersistentOnboardingStore(state)
    app.state.approvals = ApprovalService(
        store.path.with_suffix(".approvals.sqlite"), audit=app.state.audit
    )
    app.state.public_settings = None
    app.state.agent = None
    app.state.config_error = None
    try:
        settings = Settings.from_env()
        app.state.public_settings = settings
    except (ValueError, OSError) as exc:
        app.state.config_error = str(exc)
        app.state.jobs = JobService(store, guard=app.state.guard)
        try:
            yield
        finally:
            await app.state.jobs.close()
        return
    app.state.jobs = JobService(
        store,
        timeout=settings.analysis_timeout,
        guard=app.state.guard,
        config_digest=config_fingerprint(settings),
    )
    async with agent_context(
        settings, app.state.guard, app.state.audit, app.state.approvals
    ) as agent:
        app.state.agent = agent
        try:
            yield
        finally:
            await app.state.jobs.close()
            app.state.agent = None


app = FastAPI(
    title="DevLens",
    version=DEVLENS_VERSION,
    lifespan=lifespan,
    description="Evidence-first engineering analysis. Single-operator interface.",
)


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #


@app.middleware("http")
async def guard_request(request: Request, call_next):
    """Origin check, body ceiling and security headers, in that order."""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin is not None and not _same_origin(origin, request):
            return _error(403, "cross_origin", "Origin is not allowed.")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return _error(413, "payload_too_large", "Request body exceeds 1 MiB.")
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if request.url.scheme == "https":
        response.headers.setdefault(
            "strict-transport-security", "max-age=31536000; includeSubDomains"
        )
    return response


def _same_origin(origin: str, request: Request) -> bool:
    allowed = os.environ.get("DEVLENS_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if allowed:
        return origin.rstrip("/") == allowed
    host = request.headers.get("host", "")
    return origin.rstrip("/") in {
        f"{request.url.scheme}://{host}",
        f"http://{host}",
        f"https://{host}",
    }


def _error(status: int, kind: str, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "kind": kind}, status_code=status)


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def _auth(request: Request) -> AuthGate:
    gate = getattr(request.app.state, "auth", None)
    if gate is None:
        gate = AuthGate.from_env()
        request.app.state.auth = gate
    return gate


def _jobs(request: Request) -> JobService:
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is None:
        jobs = JobService(JobStore(), guard=_guard(request))
        request.app.state.jobs = jobs
    return jobs


def _approvals(request: Request) -> ApprovalService:
    store = getattr(request.app.state, "approvals", None)
    if store is None:
        store = ApprovalService(
            _jobs(request).store.path.with_suffix(".approvals.sqlite"),
            audit=getattr(request.app.state, "audit", None),
        )
        request.app.state.approvals = store
    return store


def _knowledge(request: Request) -> KnowledgeStore:
    store = getattr(request.app.state, "knowledge", None)
    if store is None:
        store = KnowledgeStore()
        request.app.state.knowledge = store
    return store


def _onboarding(request: Request) -> OnboardingStore:
    store = getattr(request.app.state, "onboarding", None)
    if store is None:
        store = OnboardingStore()
        request.app.state.onboarding = store
    return store


def _guard(request: Request | None = None) -> CapabilityGuard:
    if request is None:  # pragma: no cover - direct construction in tests
        return CapabilityGuard.from_env()
    guard = getattr(request.app.state, "guard", None)
    if guard is None:
        guard = CapabilityGuard.from_env()
        request.app.state.guard = guard
    return guard


def require_ui(request: Request) -> None:
    gate = _auth(request)
    if not gate.check(request.cookies.get(COOKIE)):
        raise HTTPException(401, "Sign in required.")


async def get_agent(request: Request):
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        detail = getattr(request.app.state, "config_error", None)
        raise HTTPException(
            503,
            f"DevLens configuration is missing or invalid: {detail}"
            if detail
            else "DevLens configuration is missing or invalid.",
        )
    return agent


def _translate(exc: BaseException) -> HTTPException:
    kind, message = classify(exc)
    return HTTPException(status_for(exc), message, headers=_retry_headers(kind))


def _retry_headers(kind: str) -> dict[str, str] | None:
    return {"Retry-After": "30"} if kind in {"capacity", "provider_unavailable"} else None


def _context(request: Request) -> RunContext:
    guard = _guard(request)
    return RunContext(
        capabilities=sorted(guard.enabled),
        model=(guard.llm.kind, guard.llm.model) if guard.llm else (None, None),
    )


# --------------------------------------------------------------------------- #
# Health and capabilities
# --------------------------------------------------------------------------- #


# HEAD as well as GET: load balancers and uptime checks routinely probe with
# HEAD, and a 404 there reads as an outage.
@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "version": DEVLENS_VERSION}


@app.api_route("/ready", methods=["GET", "HEAD"])
async def ready(agent=Depends(get_agent)):
    return {"status": "ready", "version": DEVLENS_VERSION}


@app.get("/capabilities")
async def capabilities(request: Request):
    return _guard(request).inventory()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


@app.get("/auth/status")
async def auth_status(request: Request):
    gate = _auth(request)
    return {
        "required": gate.required,
        "authenticated": gate.check(request.cookies.get(COOKIE)),
        "session_seconds": gate.ttl,
    }


@app.post("/auth/login")
async def auth_login(body: LoginRequest, request: Request):
    gate = _auth(request)
    try:
        token = gate.login(body.password, client=_client_key(request))
    except AccessDenied as exc:
        raise _translate(exc) from None
    response = JSONResponse({"required": gate.required, "authenticated": True})
    if token:
        _set_session(response, request, gate, token)
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    gate = _auth(request)
    gate.logout(request.cookies.get(COOKIE))
    response = JSONResponse(
        {"required": gate.required, "authenticated": not gate.required}
    )
    response.delete_cookie(COOKIE, path="/")
    return response


@app.post("/auth/refresh")
async def auth_refresh(request: Request, _: None = Depends(require_ui)):
    """Rotate the session token, bounding the value of a stolen cookie."""
    gate = _auth(request)
    token = gate.refresh(request.cookies.get(COOKIE))
    response = JSONResponse({"required": gate.required, "authenticated": True})
    if token:
        _set_session(response, request, gate, token)
    return response


def _set_session(response: Response, request: Request, gate: AuthGate, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=gate.ttl,
        path="/",
    )


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- #
# Projects and settings
# --------------------------------------------------------------------------- #


@app.get("/projects", response_model=list[ProjectPublic])
async def projects(request: Request, _: None = Depends(require_ui)):
    return _project_list(request)


@app.get("/settings")
async def settings_view(request: Request, _: None = Depends(require_ui)):
    settings = getattr(request.app.state, "public_settings", None)
    gate = _auth(request)
    guard = _guard(request)
    hosted = getattr(request.app.state, "hosted", False)
    return {
        "version": DEVLENS_VERSION,
        "auth_required": hosted or gate.required,
        "weak_password": gate.weak_password,
        "session_seconds": gate.ttl,
        "analyze_root": str(analyze_root()),
        "projects": [item.model_dump() for item in _project_list(request)],
        "config_error": getattr(request.app.state, "config_error", None),
        "resilience": (
            {
                "concurrency": settings.concurrency,
                "max_analyses": settings.max_analyses,
                "analysis_timeout": settings.analysis_timeout,
                "rate_limit_per_second": settings.rate_limit_per_second,
                "circuit_failure_threshold": settings.circuit_failure_threshold,
                "http_timeout": settings.http_timeout,
            }
            if settings is not None
            else None
        ),
        "capabilities": guard.inventory(),
        "model": None if hosted else os.environ.get("DEVLENS_LLM_MODEL") or None,
        "llm_provider": None if hosted else os.environ.get("DEVLENS_LLM_PROVIDER") or None,
        "llm_auth": {} if hosted else auth_methods(),
        "model_budget": guard.llm.budget.snapshot() if guard.llm else None,
    }


@app.get("/settings/{section}")
async def settings_section_view(
    section: str, request: Request, _: None = Depends(require_ui)
):
    public = await settings_view(request, None)
    audit = (getattr(request.app.state, "audit", None) or AuditLog.from_env()).recent()
    try:
        return settings_section(section, public, audit)
    except KeyError:
        raise HTTPException(404, "Settings section not found.") from None


@app.get("/audit")
async def audit_log(request: Request, limit: int = 50, _: None = Depends(require_ui)):
    audit = getattr(request.app.state, "audit", None) or AuditLog.from_env()
    return {"events": audit.recent(limit)}


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


@app.post("/jobs", response_model=JobRecord, status_code=202)
async def create_job(body: JobCreate, request: Request, _: None = Depends(require_ui)):
    try:
        request_payload(body)
        return _jobs(request).submit(getattr(request.app.state, "agent", None), body)
    except (AccessDenied, ProviderError, CapacityReached, ValueError) as exc:
        raise _translate(exc) from None


@app.get("/jobs", response_model=list[JobRecord])
async def list_jobs(
    request: Request,
    limit: int = 50,
    command: str | None = None,
    _: None = Depends(require_ui),
):
    return _jobs(request).store.list(limit=limit, command=command)


@app.get("/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str, request: Request, _: None = Depends(require_ui)):
    record = _jobs(request).store.get(job_id)
    if record is None:
        raise HTTPException(404, "Job not found.")
    return record


@app.get("/jobs/{job_id}/run")
async def get_run(job_id: str, request: Request, _: None = Depends(require_ui)):
    """Provenance for a run: versions, capabilities, and every tool call."""
    record = _jobs(request).store.get(job_id)
    if record is None or record.run_id is None:
        raise HTTPException(404, "Run not found.")
    run = _jobs(request).store.run(record.run_id)
    if run is None:
        raise HTTPException(404, "Run not found.")
    return run


@app.post("/jobs/{job_id}/cancel", response_model=JobRecord)
async def cancel_job(job_id: str, request: Request, _: None = Depends(require_ui)):
    try:
        record = _jobs(request).cancel(job_id)
    except Conflict as exc:
        raise _translate(exc) from None
    if record is None:
        raise HTTPException(404, "Job not found.")
    return record


@app.get("/jobs/{job_id}/export")
async def export_job(
    job_id: str, request: Request, format: str = "json", _: None = Depends(require_ui)
):
    record = _jobs(request).store.get(job_id)
    if record is None or record.result is None:
        raise HTTPException(404, "Job result is not available.")
    if format == "md":
        return PlainTextResponse(
            to_markdown(record.result),
            media_type="text/markdown; charset=utf-8",
            headers={"content-disposition": f'attachment; filename="devlens-{job_id}.md"'},
        )
    return JSONResponse(record.result)


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, _: None = Depends(require_ui)):
    """Server-sent events, pushed as work happens.

    Events are written by the workflow while it runs, not replayed after it
    finishes, and the stream resumes from a sequence cursor so a reconnect does
    not repeat or drop anything.
    """
    jobs = _jobs(request)
    if jobs.store.get(job_id) is None:
        raise HTTPException(404, "Job not found.")
    cursor = int(request.headers.get("last-event-id") or 0)

    async def stream():
        seen = cursor
        waiter = jobs.store.notifier.subscribe(job_id)
        try:
            while True:
                if await request.is_disconnected():
                    return
                for event in jobs.store.events(job_id, after=seen):
                    seen = event["seq"]
                    yield f"id: {seen}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n"
                record = jobs.store.get(job_id)
                if record is None or record.status not in {"queued", "running"}:
                    final = {
                        "kind": "RunFinished",
                        "message": record.status if record else "missing",
                        "data": {"error_kind": record.error_kind if record else None},
                    }
                    yield f"event: RunFinished\ndata: {json.dumps(final)}\n\n"
                    return
                waiter.clear()
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=SSE_IDLE_SECONDS)
                except TimeoutError:
                    # Nothing happened for a while; keep the connection open
                    # through proxies without inventing an event.
                    yield ": keep-alive\n\n"
        finally:
            jobs.store.notifier.unsubscribe(job_id, waiter)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-store", "x-accel-buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Proposals and approvals
# --------------------------------------------------------------------------- #


@app.get("/proposals/{run_id}")
async def list_proposals(run_id: str, request: Request, _: None = Depends(require_ui)):
    return _approvals(request).proposals_for(run_id)


@app.post("/proposals", status_code=201)
async def create_proposal(
    body: ProposalCreate, request: Request, _: None = Depends(require_ui)
):
    return _approvals(request).create_proposal(body)


@app.get("/approvals")
async def list_approvals(request: Request, _: None = Depends(require_ui)):
    return _approvals(request).list()


@app.post("/approvals", status_code=201)
async def create_approval(
    body: ApprovalCreate, request: Request, _: None = Depends(require_ui)
):
    """Request approval for an existing proposal. Free-text actions are gone."""
    try:
        return _approvals(request).request(body.proposal_id)
    except ProviderError as exc:
        raise _translate(exc) from None


@app.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str, request: Request, _: None = Depends(require_ui)):
    store = _approvals(request)
    record = store.get(approval_id)
    if record is None:
        raise HTTPException(404, "Approval not found.")
    proposal = store.proposal(record.proposal_id)
    agent = getattr(request.app.state, "agent", None)
    gateway = (
        agent.gateway_for_repository(
            proposal.repository if proposal else None,
            proposal.ticket_key if proposal else None,
        )
        if agent is not None
        else None
    )
    try:
        return await store.approve(approval_id, gateway)
    except (Conflict, ProviderError) as exc:
        raise _translate(exc) from None


@app.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, request: Request, _: None = Depends(require_ui)):
    try:
        record = _approvals(request).reject(approval_id)
    except Conflict as exc:
        raise _translate(exc) from None
    if record is None:
        raise HTTPException(404, "Approval not found.")
    return record


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


@app.post("/intent")
async def intent_detect(body: dict, _: None = Depends(require_ui)):
    return detect_intent(str(body.get("text") or "")[:4000], body.get("override"))


@app.get("/dashboard")
async def dashboard(request: Request, _: None = Depends(require_ui)):
    jobs = _jobs(request).store.list(20)
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.command] = counts.get(job.command, 0) + 1
    return {
        "recent": [item.model_dump() for item in jobs],
        "counts": counts,
        "integrations": _integrations(request),
        "capabilities": _guard(request).inventory(),
    }


@app.get("/integrations")
async def integrations(request: Request, _: None = Depends(require_ui)):
    return _integrations(request)


@app.get("/search")
async def search_all(request: Request, q: str = "", _: None = Depends(require_ui)):
    jobs = _jobs(request).store.list(100)
    return search(
        q[:200],
        jobs,
        _approvals(request).list(),
        _knowledge(request).list(),
        _integrations(request),
        _services(request, jobs),
    )


@app.get("/notifications")
async def list_notifications(request: Request, _: None = Depends(require_ui)):
    return notifications(_jobs(request).store.list(20), _approvals(request).list())


@app.get("/services")
async def list_services(request: Request, _: None = Depends(require_ui)):
    return _services(request, _jobs(request).store.list(100))


@app.get("/services/{service_id}")
async def get_service(service_id: str, request: Request, _: None = Depends(require_ui)):
    items = _services(request, _jobs(request).store.list(100))
    record = next((item for item in items if item["id"] == service_id), None)
    if record is None:
        raise HTTPException(404, "Service not found.")
    return record


@app.get("/knowledge")
async def list_knowledge(
    request: Request, category: str | None = None, _: None = Depends(require_ui)
):
    return _knowledge(request).list(category)


@app.post("/knowledge", status_code=201)
async def create_knowledge(
    body: KnowledgeCreate, request: Request, _: None = Depends(require_ui)
):
    try:
        return _knowledge(request).create(body)
    except ProviderError as exc:
        raise _translate(exc) from None


@app.get("/knowledge/{document_id}")
async def get_knowledge(document_id: str, request: Request, _: None = Depends(require_ui)):
    record = _knowledge(request).get(document_id)
    if record is None:
        raise HTTPException(404, "Document not found.")
    return record


@app.get("/onboarding")
async def onboarding_view(request: Request, _: None = Depends(require_ui)):
    settings = getattr(request.app.state, "public_settings", None)
    has_run = bool(_jobs(request).store.list(1))
    return _onboarding(request).view(
        settings is not None,
        has_run,
        bool(os.environ.get("DEVLENS_ANALYZE_ROOT", "").strip()),
    )


@app.post("/onboarding")
async def onboarding_mark(body: dict, request: Request, _: None = Depends(require_ui)):
    return _onboarding(request).mark(str(body.get("step") or "")[:60])


@app.get("/capacity")
async def capacity_view(
    events_per_day: float = 10_000_000,
    peak_multiplier: float = 10,
    payload_kb: float = 40,
    _: None = Depends(require_ui),
):
    return capacity(events_per_day, peak_multiplier, payload_kb)


# --------------------------------------------------------------------------- #
# Analyses
# --------------------------------------------------------------------------- #


@app.post(
    "/analyses/ticket", response_model=AnalysisResult, dependencies=[Depends(require_ui)]
)
async def analyze_ticket(request: AnalysisRequest, http_request: Request, agent=Depends(get_agent)):
    try:
        return await agent.analyze_ticket(request, context=_context(http_request))
    except Exception as exc:
        raise _translate(exc) from None


@app.post(
    "/analyses/review", response_model=EngineeringReport, dependencies=[Depends(require_ui)]
)
async def review(request: ReviewRequest, http_request: Request, agent=Depends(get_agent)):
    try:
        return await agent.review(request, context=_context(http_request))
    except Exception as exc:
        raise _translate(exc) from None


@app.post(
    "/analyses/implement",
    response_model=EngineeringReport,
    dependencies=[Depends(require_ui)],
)
async def implement(request: ImplementRequest, http_request: Request, agent=Depends(get_agent)):
    try:
        return await agent.implement(request, context=_context(http_request))
    except Exception as exc:
        raise _translate(exc) from None


@app.post(
    "/analyses/analyze",
    response_model=EngineeringReport,
    dependencies=[Depends(require_ui)],
)
async def analyze_workspace(request: AnalyzeRequest, http_request: Request):
    agent = getattr(http_request.app.state, "agent", None)
    context = _context(http_request)
    try:
        authorize_analyze_path(request.path)
        if agent is not None:
            return await agent.analyze(request, context=context)
        return await AnalyzeWorkflow().run(request, context=context)
    except Exception as exc:
        raise _translate(exc) from None


@app.post(
    "/analyses/ask", response_model=EngineeringReport, dependencies=[Depends(require_ui)]
)
async def ask_mode(request: PlatformRequest, http_request: Request):
    return await _platform(http_request, "ask", request)


@app.post(
    "/analyses/investigate",
    response_model=EngineeringReport,
    dependencies=[Depends(require_ui)],
)
async def investigate_mode(request: PlatformRequest, http_request: Request):
    return await _platform(http_request, "investigate", request)


@app.post(
    "/analyses/design", response_model=EngineeringReport, dependencies=[Depends(require_ui)]
)
async def design_mode(request: PlatformRequest, http_request: Request):
    return await _platform(http_request, "design", request)


@app.post(
    "/analyses/observe",
    response_model=EngineeringReport,
    dependencies=[Depends(require_ui)],
)
async def observe_mode(request: PlatformRequest, http_request: Request):
    return await _platform(http_request, "observe", request)


async def _platform(http_request: Request, command: str, request: PlatformRequest):
    agent = getattr(http_request.app.state, "agent", None)
    context = _context(http_request)
    try:
        if request.path:
            authorize_analyze_path(request.path)
        if agent is not None:
            return await getattr(agent, command)(request, context=context)
        from devlens.agent.workflows.platform import (
            AskWorkflow,
            DesignWorkflow,
            InvestigateWorkflow,
            ObserveWorkflow,
        )

        guard = _guard(http_request)
        workflows: dict[str, Any] = {
            "ask": AskWorkflow,
            "investigate": InvestigateWorkflow,
            "design": DesignWorkflow,
            "observe": ObserveWorkflow,
        }
        return await workflows[command](guard).run(request, context=context)
    except Exception as exc:
        raise _translate(exc) from None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _services(request: Request, jobs) -> list[dict]:
    settings = getattr(request.app.state, "public_settings", None)
    declared = settings.services if settings is not None else {}
    return services(jobs, _project_list(request), declared)


def _integrations(request: Request) -> list[dict]:
    settings = getattr(request.app.state, "public_settings", None)
    guard = _guard(request)
    git = (
        {project.git_provider for project in settings.projects.values()}
        if settings
        else set()
    )
    hosted = getattr(request.app.state, "hosted", False)
    status = (lambda *_: "not_configured") if hosted else _env_status
    observability = "observability_provider" in guard.enabled
    return [
        _integration("jira", "healthy" if settings else "not_configured", "read"),
        _integration("github", "healthy" if "github" in git else "not_configured", "read"),
        _integration(
            "bitbucket", "healthy" if "bitbucket_cloud" in git else "not_configured", "read"
        ),
        _integration("loki", status("DEVLENS_LOKI_URL"), "read", observability),
        _integration("prometheus", status("DEVLENS_PROM_URL"), "read", observability),
        _integration("tempo", status("DEVLENS_TEMPO_URL"), "read", observability),
        _integration("sql gateway", status("DEVLENS_SQL_URL"), "select-only", observability),
    ]


def _integration(name: str, status: str, scope: str, enabled: bool = True) -> dict:
    return {
        "name": name,
        "status": status if enabled else "not_configured",
        "scope": scope,
        "configured_by": "environment variables; DevLens has no UI connect flow",
    }


def _env_status(*keys: str) -> str:
    return (
        "healthy"
        if any(os.environ.get(key, "").strip() for key in keys)
        else "not_configured"
    )


def _project_list(request: Request) -> list[ProjectPublic]:
    settings = getattr(request.app.state, "public_settings", None)
    if settings is None:
        return []
    return [
        ProjectPublic(
            name=name,
            repositories=sorted(project.repositories),
            jira_projects=sorted(project.jira_projects),
            git_provider=project.git_provider,
        )
        for name, project in settings.projects.items()
    ]


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #


def ui_directory() -> Path:
    """Locate the built UI.

    ``DEVLENS_UI_DIR`` wins; otherwise the package's own ``web/dist`` (present in
    a wheel or container image) and finally the repository layout used during
    development. Resolving only against the repository layout is why the
    container image served no UI at all.
    """
    override = os.environ.get("DEVLENS_UI_DIR", "").strip()
    if override:
        return Path(override)
    packaged = Path(__file__).resolve().parent.parent / "web" / "dist"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def mount_ui(application: FastAPI, root: Path | None = None) -> bool:
    target = root or ui_directory()
    if target.is_dir():
        application.mount("/", StaticFiles(directory=target, html=True), name="ui")
        return True
    return False


mount_ui(app)
