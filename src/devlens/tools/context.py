"""The tool boundary.

Every call a workflow makes into the outside world goes through here, and every
call does two things before it touches the network: it checks the read policy,
and it records a :class:`~devlens.domain.ToolCall`. Authorization lives at this
boundary rather than in individual routes or workflows, so a new workflow
cannot reach a repository by forgetting a check.

Nothing here is a facade over capabilities. Gated backends are reached through
:class:`~devlens.guardrails.CapabilityGuard` directly, so there is exactly one
enforcement point per capability.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext

from devlens.domain import (
    AccessDenied,
    AnalysisRequest,
    ImplementRequest,
    ReviewRequest,
)
from devlens.policies import ReadPolicy
from devlens.providers.git.base import GitProvider
from devlens.providers.jira.base import JiraProvider
from devlens.runtime import RunContext
from devlens.sandbox.workspace import CloneSettings, Workspace, clone_repository


class ContextTools:
    """Policy-checked, recorded access to Jira, git and the sandbox."""

    def __init__(
        self,
        jira: JiraProvider,
        git: GitProvider,
        policy: ReadPolicy,
        concurrency: int = 5,
        clone: CloneSettings | None = None,
        guard=None,
    ):
        self.jira = jira
        self.git = git
        self.policy = policy
        self.clone_settings = clone
        self.slots = asyncio.Semaphore(concurrency)
        self.guard = guard

    def _record(self, context: RunContext | None, name: str, **arguments):
        if context is None:
            return nullcontext({})
        return context.tool(name, **arguments)

    # -- Jira --------------------------------------------------------------- #

    async def ticket(self, request: AnalysisRequest, context: RunContext | None = None):
        self.policy.authorize(request)
        async with self._record(
            context, "jira.get_ticket", ticket=request.ticket_key
        ) as box:
            async with self.slots:
                result = await self.jira.get_ticket(request.ticket_key)
            box["result"] = result
            return result

    async def issue_context(
        self,
        request: AnalysisRequest | ReviewRequest | ImplementRequest,
        context: RunContext | None = None,
    ):
        if not request.ticket_key:
            raise AccessDenied("A Jira ticket key is required for this workflow.")
        self.policy.authorize_ticket(request.ticket_key)
        if getattr(request, "repository", None):
            self.policy.authorize_repository(request.repository)
        async with self._record(
            context, "jira.get_issue_context", ticket=request.ticket_key
        ) as box:
            async with self.slots:
                result = await self.jira.get_issue_context(request.ticket_key)
            box["result"] = result
            return result

    # -- repository --------------------------------------------------------- #

    async def repository(
        self, request: AnalysisRequest | ImplementRequest, context: RunContext | None = None
    ):
        self.policy.authorize_repository(request.repository)
        if getattr(request, "ticket_key", None):
            self.policy.authorize_ticket(request.ticket_key)
        async with self._record(
            context,
            "git.resolve_and_list",
            repository=request.repository,
            ref=request.ref,
        ) as box:
            async with self.slots:
                commit = await self.git.resolve_ref(request.repository, request.ref)
                files = await self.git.list_files(request.repository, commit)
            box["result"] = files
            return commit, files

    async def read_file(
        self, request, commit: str, path: str, context: RunContext | None = None
    ) -> str:
        self.policy.authorize_repository(request.repository)
        async with self.slots:
            return await self.git.read_file(request.repository, commit, path)

    async def default_branch(self, repository: str, context: RunContext | None = None) -> str:
        self.policy.authorize_repository(repository)
        async with self.slots:
            return await self.git.default_branch(repository)

    # -- pull requests ------------------------------------------------------- #

    async def pull_request(self, request: ReviewRequest, context: RunContext | None = None):
        self.policy.authorize_repository(request.repository)
        if request.ticket_key:
            self.policy.authorize_ticket(request.ticket_key)
        async with self._record(
            context, "git.get_pull_request", repository=request.repository, pr=request.pr
        ) as box:
            async with self.slots:
                result = await self.git.get_pull_request(request.repository, request.pr)
            box["result"] = result
            return result

    async def pull_request_diff(
        self, request: ReviewRequest, context: RunContext | None = None
    ) -> str:
        self.policy.authorize_repository(request.repository)
        async with self._record(
            context, "git.get_diff", repository=request.repository, pr=request.pr
        ) as box:
            async with self.slots:
                result = await self.git.get_diff(request.repository, request.pr)
            box["result"] = result
            return result

    async def pull_request_commits(
        self, request: ReviewRequest, context: RunContext | None = None
    ):
        self.policy.authorize_repository(request.repository)
        async with self._record(
            context, "git.list_commits", repository=request.repository, pr=request.pr
        ) as box:
            async with self.slots:
                result = await self.git.list_commits(request.repository, request.pr)
            box["result"] = result
            return result

    # -- sandbox ------------------------------------------------------------- #

    async def open_workspace(
        self, repository: str, ref: str, context: RunContext | None = None
    ) -> Workspace:
        self.policy.authorize_repository(repository)
        if self.clone_settings is None:
            raise AccessDenied("Sandbox cloning is not configured for this project.")
        async with self._record(
            context, "sandbox.clone", repository=repository, ref=ref
        ) as box:
            workspace = await clone_repository(self.clone_settings, repository, ref)
            box["result"] = str(workspace.root)
            return workspace
