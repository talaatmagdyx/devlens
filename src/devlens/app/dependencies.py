"""Wiring.

Each project gets its own HTTP clients, its own read policy and its own
capability-scoped write gateway. Resilience policies are allocated **per
upstream host**, so a Jira outage cannot open the circuit for GitHub.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx

from devlens.agent.audit import AuditLog
from devlens.agent.engineering_agent import WorkflowRunner
from devlens.agent.workflows.analyze import AnalyzeWorkflow
from devlens.agent.workflows.implementation import ImplementWorkflow
from devlens.agent.workflows.review import ReviewWorkflow
from devlens.agent.workflows.ticket import TicketWorkflow
from devlens.app.config import Settings
from devlens.domain import (
    AccessDenied,
    AnalysisRequest,
    AnalyzeRequest,
    ImplementRequest,
    ReviewRequest,
)
from devlens.guardrails import CapabilityGuard
from devlens.policies import ReadPolicy
from devlens.providers.factory import GitProviderFactory, JiraProviderFactory, git_strategy
from devlens.providers.writes import WriteGateway
from devlens.resilience import PolicyRegistry
from devlens.runtime import RunContext
from devlens.sandbox.workspace import CloneSettings
from devlens.tools.context import ContextTools


class ProjectRunners:
    """Routes a request to the runner for its project, under global limits."""

    def __init__(
        self,
        runners: dict[str, WorkflowRunner],
        settings: Settings,
        gateways: dict[str, WriteGateway] | None = None,
        guard: CapabilityGuard | None = None,
    ):
        self.runners = runners
        self.slots = asyncio.Semaphore(settings.max_analyses)
        self.timeout = settings.analysis_timeout
        self.gateways = gateways or {}
        self.guard = guard

    def gateway_for(self, project: str = "default") -> WriteGateway | None:
        return self.gateways.get(project)

    def gateway_for_repository(self, repository: str | None, ticket: str | None):
        """Find the gateway whose project owns this resource, or None."""
        for _project, gateway in self.gateways.items():
            policy = gateway.policy
            if policy is None:
                continue
            try:
                if repository:
                    policy.authorize_repository(repository)
                elif ticket:
                    policy.authorize_ticket(ticket)
                else:
                    continue
                return gateway
            except AccessDenied:
                continue
        return None

    async def analyze_ticket(self, request: AnalysisRequest, context=None):
        return await self._run(request.project, "analyze_ticket", request, context)

    async def review(self, request: ReviewRequest, context=None):
        return await self._run(request.project, "review", request, context)

    async def implement(self, request: ImplementRequest, context=None):
        return await self._run(request.project, "implement", request, context)

    async def analyze(self, request: AnalyzeRequest, context: RunContext | None = None):
        async with asyncio.timeout(self.timeout), self.slots:
            runner = next(iter(self.runners.values()), None)
            if runner is not None:
                return await runner.analyze(request, context=context)
            return await AnalyzeWorkflow().run(request, context=context)

    async def ask(self, request, context=None):
        return await self._run(request.project, "ask", request, context)

    async def investigate(self, request, context=None):
        return await self._run(request.project, "investigate", request, context)

    async def design(self, request, context=None):
        return await self._run(request.project, "design", request, context)

    async def observe(self, request, context=None):
        return await self._run(request.project, "observe", request, context)

    async def _run(self, project: str, method: str, request, context):
        runner = self.runners.get(project)
        if runner is None:
            known = ", ".join(sorted(self.runners)) or "none"
            raise AccessDenied(
                f"Project {project!r} is not configured (configured: {known})."
            )
        async with asyncio.timeout(self.timeout), self.slots:
            return await getattr(runner, method)(request, context=context)


def _clone_settings(provider: str, token: str, email: str) -> CloneSettings:
    return git_strategy(provider).clone(token, email)


@asynccontextmanager
async def agent_context(
    settings: Settings,
    guard: CapabilityGuard | None = None,
    audit: AuditLog | None = None,
    approvals=None,
):
    audit = audit if audit is not None else AuditLog.from_env()
    guard = guard or CapabilityGuard.from_env()
    registry = PolicyRegistry(
        settings.rate_limit_per_second,
        settings.rate_limit_burst,
        settings.circuit_failure_threshold,
        settings.circuit_recovery_seconds,
        settings.http_timeout,
    )
    async with AsyncExitStack() as stack:
        runners: dict[str, WorkflowRunner] = {}
        gateways: dict[str, WriteGateway] = {}
        # httpx types every keyword separately, so a shared mapping of client
        # options cannot be expressed as one of those overloads.
        options: dict[str, Any] = {
            "timeout": httpx.Timeout(
                settings.http_timeout, connect=settings.http_connect_timeout
            ),
            "follow_redirects": False,
            "limits": httpx.Limits(
                max_connections=settings.concurrency,
                max_keepalive_connections=settings.concurrency,
            ),
        }
        for name, project in settings.projects.items():
            jira = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=project.jira_url,
                    auth=(project.jira_email, project.jira_token.get_secret_value()),
                    headers={"User-Agent": "devlens/0.2"},
                    **options,
                )
            )
            registry.attach_to(jira)
            token = project.git_token.get_secret_value()
            git = await stack.enter_async_context(
                git_strategy(project.git_provider).client(
                    token, project.git_email, **options
                )
            )
            registry.attach_to(git)

            provider = GitProviderFactory.create(project.git_provider, git)
            jira_provider = JiraProviderFactory.create(jira)
            policy = ReadPolicy(project.repositories, project.jira_projects)
            gateway = WriteGateway(
                git=provider, jira=jira_provider, guard=guard, policy=policy
            )
            gateways[name] = gateway
            tools = ContextTools(
                jira_provider,
                provider,
                policy,
                settings.concurrency,
                _clone_settings(project.git_provider, token, project.git_email),
                guard=guard,
            )
            runners[name] = WorkflowRunner(
                TicketWorkflow(tools),
                review_workflow=ReviewWorkflow(tools),
                implement_workflow=ImplementWorkflow(tools),
                audit=audit,
                guard=guard,
                approvals=approvals,
                tools=tools,
            )
        yield ProjectRunners(runners, settings, gateways=gateways, guard=guard)


#: Retained so existing imports keep working.
ProjectAgents = ProjectRunners
