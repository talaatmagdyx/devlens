import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from devlens.agent.context_builder import build_context
from devlens.app.config import ProjectSettings, Settings
from devlens.app.dependencies import ProjectAgents, agent_context
from devlens.domain import (
    AccessDenied,
    AnalysisRequest,
    ProviderError,
    RepositoryFile,
    Ticket,
)
from devlens.policies import ReadPolicy
from devlens.providers.git.bitbucket_cloud import BitbucketCloudProvider
from devlens.tools.context import ContextTools

SHA = "b" * 40


async def test_bitbucket_default_branch_nested_tree_pagination_and_raw_file():
    calls = []
    root = f"/2.0/repositories/team/payments/src/{SHA}/"

    async def handler(request):
        calls.append(request)
        path = request.url.path
        if path == "/2.0/repositories/team/payments":
            return httpx.Response(200, json={"mainbranch": {"name": "main"}})
        if "/commit/" in path:
            return httpx.Response(200, json={"hash": SHA})
        if path == root and "page" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "values": [{"type": "commit_directory", "path": "src"}],
                    "next": f"https://api.bitbucket.org{root}?page=2",
                },
            )
        if path == root:
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "type": "commit_file",
                            "path": "link.py",
                            "size": 10,
                            "attributes": ["link"],
                        }
                    ]
                },
            )
        if path == root + "src/":
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "type": "commit_file",
                            "path": "src/payments.py",
                            "size": 20,
                            "attributes": [],
                        }
                    ]
                },
            )
        assert path == root + "src/payments.py"
        return httpx.Response(200, text="def payments(): pass")

    async with httpx.AsyncClient(
        base_url="https://api.bitbucket.org/2.0/",
        transport=httpx.MockTransport(handler),
    ) as client:
        provider = BitbucketCloudProvider(client)
        assert await provider.resolve_ref("team/payments", "HEAD") == SHA
        assert await provider.list_files("team/payments", SHA) == [
            RepositoryFile(path="src/payments.py", size=20)
        ]
        assert (
            await provider.read_file("team/payments", SHA, "src/payments.py")
            == "def payments(): pass"
        )
        assert provider.evidence_url(
            "team/payments", SHA, "src/payments.py", 1
        ).endswith(f"/src/{SHA}/src/payments.py#lines-1")
    assert len(calls) == 6


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/leak",
        "https://api.bitbucket.org/2.0/repositories/other/private/src/main/",
    ],
)
async def test_bitbucket_rejects_unsafe_pagination(next_url):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"values": [], "next": next_url})

    async with httpx.AsyncClient(
        base_url="https://api.bitbucket.org/2.0/",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProviderError, match="unsafe"):
            await BitbucketCloudProvider(client).list_files("team/payments", SHA)
    assert len(calls) == 1


async def test_bitbucket_rejects_oversize_raw_content():
    async with httpx.AsyncClient(
        base_url="https://api.bitbucket.org/2.0/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"a" * 100_001)
        ),
    ) as client:
        with pytest.raises(ProviderError, match="size"):
            await BitbucketCloudProvider(client).read_file(
                "team/payments", SHA, "big.py"
            )


class ConcurrentGit:
    name = "bitbucket_cloud"

    def __init__(self):
        self.active = 0
        self.peak = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve_ref(self, repository, ref):
        return SHA

    async def list_files(self, repository, commit):
        return [RepositoryFile(path=f"payments_{i}.py", size=20) for i in range(10)]

    async def read_file(self, repository, commit, path):
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == 3:
            self.started.set()
        try:
            await self.release.wait()
            return "payments = True"
        finally:
            self.active -= 1

    def evidence_url(self, repository, commit, path, line):
        return f"https://bitbucket.org/{repository}/src/{commit}/{path}#lines-{line}"


def context(git):
    tools = ContextTools(
        AsyncMock(),
        git,
        ReadPolicy(frozenset({"team/payments"}), frozenset({"DEV"})),
        concurrency=3,
    )
    request = AnalysisRequest(ticket_key="DEV-1", repository="team/payments")
    ticket = Ticket(
        key="DEV-1",
        summary="payments",
        description="",
        url="https://jira.example/DEV-1",
    )
    return tools, request, ticket


async def test_concurrency_is_shared_across_analyses_and_results_are_stable():
    git = ConcurrentGit()
    tools, request, ticket = context(git)
    tasks = [
        asyncio.create_task(build_context(tools, request, ticket)) for _ in range(2)
    ]
    await asyncio.wait_for(git.started.wait(), timeout=1)
    assert git.peak == 3
    git.release.set()
    first, second = await asyncio.gather(*tasks)
    assert first == second
    assert git.peak == 3
    assert git.active == 0
    assert len(first[1]) == 10


async def test_cancellation_releases_all_file_slots():
    git = ConcurrentGit()
    tools, request, ticket = context(git)
    task = asyncio.create_task(build_context(tools, request, ticket))
    await asyncio.wait_for(git.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert git.active == 0
    git.release.set()
    assert (
        len(
            (await asyncio.wait_for(build_context(tools, request, ticket), timeout=1))[
                1
            ]
        )
        == 10
    )


def settings():
    def project(provider, repo, token):
        return ProjectSettings(
            jira_url="https://team.atlassian.net",
            jira_email="user@example.com",
            jira_token="jira-test",
            git_provider=provider,
            git_token=token,
            repositories={repo},
            jira_projects={"DEV"},
        )

    return Settings(
        projects={
            "payments": project("bitbucket_cloud", "team/payments", "bb-test"),
            "platform": project("github", "org/platform", "gh-test"),
        }
    )


async def test_project_clients_are_isolated_reused_and_closed():
    async with agent_context(settings()) as registry:
        bb = registry.agents["payments"].ticket_workflow.tools
        gh = registry.agents["platform"].ticket_workflow.tools
        assert bb.git.name == "bitbucket_cloud"
        assert gh.git.name == "github"
        assert bb.git.client is not gh.git.client
        assert bb.git.client.headers["Authorization"] == "Bearer bb-test"
        assert gh.git.client.headers["Authorization"] == "Bearer gh-test"
        assert (
            registry.agents["payments"].ticket_workflow.tools.git.client
            is bb.git.client
        )
        with pytest.raises(AccessDenied):
            await registry.analyze_ticket(
                AnalysisRequest(
                    project="payments", ticket_key="DEV-1", repository="org/platform"
                )
            )
        with pytest.raises(AccessDenied):
            await registry.analyze_ticket(
                AnalysisRequest(
                    project="missing", ticket_key="DEV-1", repository="team/payments"
                )
            )
    assert (
        bb.git.client.is_closed and gh.git.client.is_closed and bb.jira.client.is_closed
    )


async def test_analysis_deadline_cancels_work():
    config = settings().model_copy(update={"analysis_timeout": 0.02})
    finished = asyncio.Event()

    async def slow(request):
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    registry = ProjectAgents({"payments": AsyncMock(analyze_ticket=slow)}, config)
    with pytest.raises(TimeoutError):
        await registry.analyze_ticket(
            AnalysisRequest(
                project="payments", ticket_key="DEV-1", repository="team/payments"
            )
        )
    assert finished.is_set()


def test_multiple_projects_toml_and_secret_env(tmp_path, monkeypatch):
    path = tmp_path / "devlens.toml"
    path.write_text("""concurrency = 3
[projects.payments]
jira_url = "https://team.atlassian.net"
jira_email = "user@example.com"
jira_token_env = "TEST_JIRA_TOKEN"
git_provider = "bitbucket_cloud"
git_email = "user@example.com"
git_token_env = "TEST_BB_TOKEN"
repositories = ["team/payments", "team/ledger"]
jira_projects = ["PAY", "LEDGER"]
""")
    monkeypatch.setenv("DEVLENS_CONFIG", str(path))
    monkeypatch.setenv("TEST_JIRA_TOKEN", "jira-secret")
    monkeypatch.setenv("TEST_BB_TOKEN", "bb-secret")
    config = Settings.from_env()
    assert config.concurrency == 3
    assert len(config.projects["payments"].repositories) == 2
    assert "bb-secret" not in repr(config)
    monkeypatch.delenv("TEST_BB_TOKEN")
    with pytest.raises(ValueError, match="missing"):
        Settings.from_env()
