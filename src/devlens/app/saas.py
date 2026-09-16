"""Hosted, multi-tenant entry point.

This is a second, stricter front door for running DevLens for more than one
operator. It is not the default: ``devlens.app.api:app`` is what a single
engineer runs locally. Use this one when DevLens is reachable by more than the
person who started it.

What it adds over the local application: bearer or ``__Host-`` cookie identities
with expiry, per-tenant request and concurrency quotas, an origin check on every
browser write, HTTPS enforcement, a request-body ceiling, security headers, a
structured access log that deliberately records no bodies or credentials, and
one isolated application — with its own job store, approval queue and audit log
— per tenant. Host paths, implementation and repository code execution are
refused outright until an isolated runner exists.

One worker per data directory is enforced with a file lock rather than assumed.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import time
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from devlens.agent.audit import AuditLog
from devlens.app.api import app as local_app
from devlens.app.api import mount_ui
from devlens.app.approvals import ApprovalService
from devlens.app.auth import AuthGate
from devlens.app.config import Settings
from devlens.app.dependencies import agent_context
from devlens.app.hosting import HostingConfig
from devlens.app.identity import SESSION_COOKIE, IdentityStore, Principal, authenticate
from devlens.app.jobs import JobService, JobStore, config_fingerprint
from devlens.app.persistence import (
    PersistentKnowledgeStore,
    PersistentOnboardingStore,
    SQLiteStateRepository,
)
from devlens.domain import AccessDenied
from devlens.guardrails import CapabilityGuard

log = logging.getLogger("devlens.http")

PUBLIC = frozenset({"/health", "/ready", "/auth/status", "/auth/login", "/auth/logout"})
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Commands safe to expose to a tenant. Anything that touches a host path or
#: executes repository code stays off until an isolated runner exists.
HOSTED_COMMANDS = frozenset({"ticket", "review", "ask", "investigate", "design", "observe"})
MAX_BODY = 65_536
SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
)


class RequestQuota:
    """Fixed-window request quota."""

    def __init__(self, limit: int, seconds: float = 60):
        self.limit, self.seconds = limit, seconds
        self.events: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.events and self.events[0] <= now - self.seconds:
            self.events.popleft()
        if len(self.events) >= self.limit:
            return False
        self.events.append(now)
        return True


class HostedPolicy:
    """Deny host paths and code execution until an isolated runner exists."""

    @staticmethod
    def authorize(
        principal: Principal, path: str, method: str, payload: dict, projects: set[str]
    ) -> None:
        if method not in READ_METHODS and principal.role != "operator":
            raise AccessDenied("Operator role required.")
        if method not in READ_METHODS and path.startswith(("/approvals", "/proposals")):
            raise AccessDenied("Hosted remote writes are disabled.")
        if path.startswith("/analyses/") or (path == "/jobs" and method == "POST"):
            command = payload.get("command") if path == "/jobs" else path.rsplit("/", 1)[-1]
            if (
                command not in HOSTED_COMMANDS
                or payload.get("path")
                or payload.get("run_tests")
            ):
                raise AccessDenied(
                    "Host paths, implementation and repository code execution "
                    "require an isolated runner."
                )
            if payload.get("project", "default") not in projects:
                raise AccessDenied("Project is not available to this tenant.")


class TenantRuntime:
    def __init__(self, application: FastAPI, projects: set[str], quota: RequestQuota):
        self.app, self.projects, self.quota = application, projects, quota
        self.inflight = 0


@asynccontextmanager
async def hosted_lifespan(application: FastAPI):
    # Configuration errors deliberately fail startup; hosted mode never falls
    # back to open access.
    config = HostingConfig.from_env()
    settings = Settings.from_env()
    identities = IdentityStore(config)
    declared = set().union(*(tenant.projects for tenant in config.tenants.values()))
    if not declared <= settings.projects.keys():
        raise ValueError("A tenant references an unconfigured project.")
    application.state.hosting = config
    application.state.identities = identities
    application.state.tenants = {}
    application.state.login_quota = RequestQuota(20)
    async with AsyncExitStack() as stack:
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = stack.enter_context((config.data_dir / "worker.lock").open("a"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Hosted SQLite mode supports one worker per data directory."
            ) from None
        for tenant_id, tenant in config.tenants.items():
            root = config.data_dir / tenant_id
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            child = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
            # Route implementations are shared; request.app and every mutable
            # store are per tenant.
            child.router.routes = [
                route
                for route in local_app.router.routes
                if getattr(route, "path", "")
                not in PUBLIC | {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
            ]
            scoped = settings.model_copy(
                update={
                    "projects": {
                        name: settings.projects[name] for name in tenant.projects
                    }
                }
            )
            audit = AuditLog(root / "audit.jsonl")
            guard = CapabilityGuard()
            approvals = ApprovalService(root / "approvals.sqlite", audit=audit)
            agent = await stack.enter_async_context(
                agent_context(scoped, guard, audit, approvals)
            )
            store = JobStore(root / "jobs.sqlite")
            store.recover_interrupted()
            jobs = JobService(
                store,
                timeout=settings.analysis_timeout,
                guard=guard,
                config_digest=config_fingerprint(scoped),
            )
            stack.push_async_callback(jobs.close)
            state = SQLiteStateRepository(root / "state.sqlite")
            child.state.auth = AuthGate()
            child.state.jobs = jobs
            child.state.approvals = approvals
            child.state.knowledge = PersistentKnowledgeStore(state)
            child.state.onboarding = PersistentOnboardingStore(state)
            child.state.audit = audit
            child.state.guard = guard
            child.state.agent = agent
            child.state.public_settings = scoped
            child.state.config_error = None
            child.state.hosted = True
            application.state.tenants[tenant_id] = TenantRuntime(
                child, set(tenant.projects), RequestQuota(config.requests_per_minute)
            )
        application.state.ready = True
        try:
            yield
        finally:
            application.state.ready = False
    application.state.tenants.clear()


class HostedGateway:
    """ASGI gateway: authenticate, authorise, quota, then dispatch to a tenant."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        owner = scope["app"]
        request_id = str(uuid4())
        status = 500
        principal = None
        started = time.monotonic()

        async def tagged(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                    *SECURITY_HEADERS,
                ]
            await send(message)

        async def respond(code, detail, headers=None):
            await JSONResponse({"detail": detail}, status_code=code, headers=headers)(
                scope, receive, tagged
            )

        try:
            if request.url.path == "/health":
                return await JSONResponse({"status": "ok"})(scope, receive, tagged)
            if not getattr(owner.state, "ready", False):
                return await respond(503, "Service is not ready.")
            if request.url.path == "/ready":
                return await JSONResponse({"status": "ready"})(scope, receive, tagged)
            config = owner.state.hosting
            store = owner.state.identities
            if scope["scheme"] != "https":
                return await respond(400, "HTTPS is required.")
            principal = authenticate(request, store)
            if request.url.path == "/auth/status":
                return await JSONResponse(
                    {"required": True, "authenticated": principal is not None}
                )(scope, receive, tagged)
            login = request.url.path == "/auth/login" and request.method == "POST"
            if request.method not in READ_METHODS:
                origin = request.headers.get("origin")
                if origin is not None and origin != config.public_origin:
                    return await respond(403, "Origin is not allowed.")
                if (
                    principal is not None
                    and "authorization" not in request.headers
                    and not login
                    and origin != config.public_origin
                ):
                    return await respond(403, "Origin is required for browser writes.")
                if principal is None and not login and "authorization" not in request.headers:
                    return await respond(
                        401, "Sign in required.", {"WWW-Authenticate": "Bearer"}
                    )
            if request.url.path == "/auth/logout" and request.method == "POST":
                store.revoke(request.cookies.get(SESSION_COOKIE, ""))
                response = JSONResponse({"required": True, "authenticated": False})
                response.delete_cookie(
                    SESSION_COOKIE, secure=True, httponly=True, samesite="strict", path="/"
                )
                return await response(scope, receive, tagged)
            if login and not owner.state.login_quota.allow():
                return await respond(429, "Too many login attempts.", {"Retry-After": "60"})
            if principal is None and not login:
                # The compiled public UI contains no tenant data.
                if request.method == "GET" and (
                    request.url.path == "/" or request.url.path.startswith("/assets/")
                ):
                    return await self.app(scope, receive, tagged)
                return await respond(401, "Sign in required.", {"WWW-Authenticate": "Bearer"})
            body = bytearray()
            async with asyncio.timeout(10):
                more = True
                while more:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_BODY:
                        return await respond(413, "Request body exceeds 64 KiB.")
                    more = message.get("more_body", False)
            payload = json.loads(body) if body else {}
            if not isinstance(payload, dict):
                return await respond(422, "Expected a JSON object.")
            if login:
                password = payload.get("password", "")
                principal = store.bearer(password) if isinstance(password, str) else None
                if principal is None:
                    return await respond(401, "Invalid credentials.")
                try:
                    session = store.issue(principal)
                except ValueError:
                    return await respond(429, "Session capacity reached.")
                response = JSONResponse({"required": True, "authenticated": True})
                response.set_cookie(
                    SESSION_COOKIE,
                    session,
                    max_age=store.ttl,
                    secure=True,
                    httponly=True,
                    samesite="strict",
                    path="/",
                )
                return await response(scope, receive, tagged)
            if principal is None:  # pragma: no cover - the checks above return first
                return await respond(401, "Sign in required.")
            runtime = owner.state.tenants[principal.tenant]
            HostedPolicy.authorize(
                principal,
                request.url.path.rstrip("/"),
                request.method,
                payload,
                runtime.projects,
            )
            if not runtime.quota.allow() or runtime.inflight >= config.max_inflight_per_tenant:
                return await respond(
                    429, "Tenant request limit reached.", {"Retry-After": "60"}
                )
            runtime.inflight += 1
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            try:
                await runtime.app(dict(scope), replay, tagged)
            finally:
                runtime.inflight -= 1
        except (json.JSONDecodeError, UnicodeError):
            await respond(400, "Invalid JSON.")
        except AccessDenied as exc:
            await respond(403, str(exc))
        except TimeoutError:
            await respond(408, "Request body timed out.")
        finally:
            # Never record cookies, credentials, query strings, bodies or
            # source excerpts.
            log.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "tenant": getattr(principal, "tenant", None),
                        "subject": getattr(principal, "subject", None),
                        "method": request.method,
                        "path": request.url.path,
                        "status": status,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    }
                )
            )


app = FastAPI(
    title="DevLens Hosted",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=hosted_lifespan,
)
app.add_middleware(HostedGateway)
# Only compiled static assets are served directly; every API call goes through
# the gateway.
mount_ui(app)
