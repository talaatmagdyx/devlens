from unittest.mock import AsyncMock

import httpx
import pytest

from devlens.app.api import app, get_agent
from devlens.app.config import Settings


def configure(monkeypatch):
    monkeypatch.delenv("DEVLENS_CONFIG", raising=False)
    for key, value in {
        "JIRA_URL": "https://team.atlassian.net",
        "JIRA_EMAIL": "user@example.com",
        "JIRA_TOKEN": "jira-test",
        "GIT_PROVIDER": "bitbucket_cloud",
        "BITBUCKET_EMAIL": "user@example.com",
        "BITBUCKET_TOKEN": "bb-test",
        "REPOSITORIES": "team/payments,team/ledger",
        "JIRA_PROJECTS": "PAY,LEDGER",
    }.items():
        monkeypatch.setenv("DEVLENS_" + key, value)


async def test_lifespan_reuses_clients_and_closes_them(monkeypatch):
    configure(monkeypatch)
    async with app.router.lifespan_context(app):
        registry = app.state.agent
        tools = registry.agents["default"].ticket_workflow.tools
        assert tools.git.name == "bitbucket_cloud"
        assert len(Settings.from_env().projects["default"].repositories) == 2
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get("/ready")).status_code == 200
            assert (await client.get("/ready")).status_code == 200
        assert app.state.agent is registry
        assert not tools.git.client.is_closed
    assert tools.git.client.is_closed
    assert app.state.agent is None


async def test_invalid_config_exposes_liveness_but_not_readiness(monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv("DEVLENS_BITBUCKET_TOKEN")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/ready")).status_code == 503


async def test_deadline_maps_to_504():
    app.dependency_overrides[get_agent] = lambda: AsyncMock(
        analyze_ticket=AsyncMock(side_effect=TimeoutError)
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/analyses/ticket",
                json={"ticket_key": "DEV-1", "repository": "org/repo"},
            )
            assert response.status_code == 504
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("concurrency", [0, 33])
def test_invalid_concurrency_rejected(monkeypatch, concurrency):
    configure(monkeypatch)
    with pytest.raises(ValueError):
        Settings(projects=Settings.from_env().projects, concurrency=concurrency)
