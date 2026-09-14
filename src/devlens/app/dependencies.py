import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import httpx

from devlens.agent.engineering_agent import EngineeringAgent
from devlens.agent.workflows.ticket import TicketWorkflow
from devlens.app.config import Settings
from devlens.domain import AccessDenied, AnalysisRequest
from devlens.policies import ReadPolicy
from devlens.providers.git.github import GitHubProvider
from devlens.providers.git.bitbucket_cloud import BitbucketCloudProvider
from devlens.providers.jira.cloud import JiraCloudProvider
from devlens.tools.context import ContextTools


class ProjectAgents:
    def __init__(self, agents: dict[str, EngineeringAgent], settings: Settings):
        self.agents = agents
        self.slots = asyncio.Semaphore(settings.max_analyses)
        self.timeout = settings.analysis_timeout

    async def analyze_ticket(self, request: AnalysisRequest):
        agent = self.agents.get(request.project)
        if agent is None:
            raise AccessDenied("Project is not configured.")
        async with asyncio.timeout(self.timeout):
            async with self.slots:
                return await agent.analyze_ticket(request)


@asynccontextmanager
async def agent_context(settings: Settings):
    async with AsyncExitStack() as stack:
        agents = {}
        for name, project in settings.projects.items():
            options = dict(
                timeout=httpx.Timeout(15, connect=5),
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=settings.concurrency,
                    max_keepalive_connections=settings.concurrency,
                ),
            )
            jira = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=project.jira_url,
                    auth=(project.jira_email, project.jira_token.get_secret_value()),
                    **options,
                )
            )
            if project.git_provider == "github":
                git = await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url="https://api.github.com/",
                        headers={
                            "Authorization": f"Bearer {project.git_token.get_secret_value()}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        **options,
                    )
                )
                provider = GitHubProvider(git)
            else:
                auth = (
                    {"auth": (project.git_email, project.git_token.get_secret_value())}
                    if project.git_email
                    else {
                        "headers": {
                            "Authorization": f"Bearer {project.git_token.get_secret_value()}"
                        }
                    }
                )
                git = await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url="https://api.bitbucket.org/2.0/", **auth, **options
                    )
                )
                provider = BitbucketCloudProvider(git)
            agents[name] = EngineeringAgent(
                TicketWorkflow(
                    ContextTools(
                        JiraCloudProvider(jira),
                        provider,
                        ReadPolicy(project.repositories, project.jira_projects),
                        settings.concurrency,
                    )
                )
            )
        yield ProjectAgents(agents, settings)
