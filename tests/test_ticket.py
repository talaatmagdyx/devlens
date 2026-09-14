import base64

import httpx
import pytest

from devlens.agent.engineering_agent import EngineeringAgent
from devlens.agent.workflows.ticket import TicketWorkflow
from devlens.app.api import app, get_agent
from devlens.domain import AccessDenied, AnalysisRequest, ProviderError
from devlens.policies import ReadPolicy
from devlens.providers.git.github import GitHubProvider
from devlens.providers.jira.cloud import JiraCloudProvider
from devlens.tools.context import ContextTools

SHA = "a" * 40


@pytest.fixture
async def rig():
    calls = []
    state = {
        "truncated": False,
        "status": 200,
        "content": "def checkout():\n    return retry_payment()\n",
    }

    def handler(request):
        calls.append(request)
        if state["status"] != 200:
            return httpx.Response(state["status"], text="secret provider diagnostic")
        if "/issue/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "summary": "Checkout failure",
                        "description": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "Inspect checkout"}
                                    ],
                                }
                            ],
                        },
                    }
                },
            )
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": SHA})
        if "/git/trees/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "truncated": state["truncated"],
                    "tree": [
                        {
                            "path": "src/checkout.py",
                            "size": 50,
                            "mode": "100755",
                            "type": "blob",
                        },
                        {"path": ".env", "size": 10, "mode": "100644", "type": "blob"},
                        {
                            "path": "linked.py",
                            "size": 10,
                            "mode": "120000",
                            "type": "blob",
                        },
                    ],
                },
            )
        assert request.url.params["ref"] == SHA
        return httpx.Response(
            200,
            json={
                "encoding": "base64",
                "size": len(state["content"]),
                "content": base64.b64encode(state["content"].encode()).decode(),
            },
        )

    transport = httpx.MockTransport(handler)
    async with (
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/", transport=transport
        ) as jira,
        httpx.AsyncClient(
            base_url="https://api.github.com/", transport=transport
        ) as git,
    ):
        tools = ContextTools(
            JiraCloudProvider(jira),
            GitHubProvider(git),
            ReadPolicy(frozenset({"org/repo"}), frozenset({"DEV"})),
        )
        yield EngineeringAgent(TicketWorkflow(tools)), calls, state


def request(**kwargs):
    return AnalysisRequest(
        **{"ticket_key": "DEV-1", "repository": "org/repo", **kwargs}
    )


async def test_ticket_to_pinned_evidence(rig):
    agent, calls, _ = rig
    result = await agent.analyze_ticket(request())
    assert result.status == "completed"
    assert result.ticket.description == "Inspect checkout"
    assert result.evidence[0].line == 1
    assert (
        result.evidence[0].url
        == f"https://github.com/org/repo/blob/{SHA}/src/checkout.py#L1"
    )
    assert len(calls) == 4


@pytest.mark.parametrize(
    "kwargs", [{"repository": "other/repo"}, {"ticket_key": "OTHER-1"}]
)
async def test_access_denied_before_network(rig, kwargs):
    agent, calls, _ = rig
    with pytest.raises(AccessDenied):
        await agent.analyze_ticket(request(**kwargs))
    assert not calls


async def test_truncated_tree_fails_explicitly(rig):
    agent, _, state = rig
    state["truncated"] = True
    with pytest.raises(ProviderError, match="exceeds"):
        await agent.analyze_ticket(request())


async def test_upstream_error_does_not_leak_body(rig):
    agent, _, state = rig
    state["status"] = 401
    with pytest.raises(ProviderError) as exc:
        await agent.analyze_ticket(request())
    assert str(exc.value) == "Provider request failed (HTTP 401)."


async def test_no_evidence_is_insufficient(rig):
    agent, _, state = rig
    state["content"] = "x = 1"
    assert (await agent.analyze_ticket(request())).status == "insufficient_context"


async def test_api(rig):
    agent, _, _ = rig
    app.dependency_overrides[get_agent] = lambda: agent
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (
                await client.post("/analyses/ticket", json=request().model_dump())
            ).json()["status"] == "completed"
            assert (
                await client.post(
                    "/analyses/ticket",
                    json=request(repository="other/repo").model_dump(),
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/analyses/ticket",
                    json={"ticket_key": "../bad", "repository": "org/repo"},
                )
            ).status_code == 422
    finally:
        app.dependency_overrides.clear()
