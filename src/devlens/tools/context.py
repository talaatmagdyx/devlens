import asyncio

from devlens.domain import AnalysisRequest
from devlens.policies import ReadPolicy
from devlens.providers.git.base import GitProvider
from devlens.providers.jira.base import JiraProvider


class ContextTools:
    def __init__(
        self,
        jira: JiraProvider,
        git: GitProvider,
        policy: ReadPolicy,
        concurrency: int = 5,
    ):
        self.jira, self.git, self.policy = jira, git, policy
        self.slots = asyncio.Semaphore(concurrency)

    async def ticket(self, request: AnalysisRequest):
        self.policy.authorize(request)
        async with self.slots:
            return await self.jira.get_ticket(request.ticket_key)

    async def repository(self, request: AnalysisRequest):
        self.policy.authorize(request)
        async with self.slots:
            commit = await self.git.resolve_ref(request.repository, request.ref)
            return commit, await self.git.list_files(request.repository, commit)

    async def read_file(self, request: AnalysisRequest, commit: str, path: str):
        self.policy.authorize(request)
        async with self.slots:
            return await self.git.read_file(request.repository, commit, path)
