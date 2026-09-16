"""The rest of the HTTP surface.

Every route the UI calls, exercised once, because an endpoint nothing covers is
an endpoint nobody has run. Two properties matter throughout: a route that
depends on an unconfigured backend answers with a reason rather than a 500, and
no route returns a credential.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from devlens.agent.audit import AuditLog
from devlens.app.api import app
from devlens.app.api import app as application
from devlens.app.approvals import ApprovalService
from devlens.app.auth import AuthGate
from devlens.app.catalog import KnowledgeStore, OnboardingStore
from devlens.app.config import Settings
from devlens.app.jobs import JobCreate, JobService, JobStore
from devlens.domain import AgentEvent
from devlens.guardrails import CapabilityGuard


class StubAgent:
    async def analyze_ticket(self, request, context=None):
        from devlens.domain import AnalysisResult, Ticket

        return AnalysisResult(
            project=request.project,
            provider="github",
            status="completed",
            method="lexical",
            repository=request.repository,
            commit="a" * 40,
            requested_ref=request.ref,
            summary="analysed",
            ticket=Ticket(
                key=request.ticket_key,
                summary="Checkout times out",
                description="",
                url="https://team.atlassian.net/browse/DEV-1",
                snapshot_at=time.time(),
            ),
        )

    async def ask(self, payload, context=None):
        from devlens.domain import EngineeringReport

        return EngineeringReport(task="ask", status="completed", executive_summary="ok")

    analyze = investigate = observe = design = ask

    def gateway_for_repository(self, repository, ticket_key):
        return None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVLENS_JIRA_URL", "https://team.atlassian.net")
    monkeypatch.setenv("DEVLENS_JIRA_EMAIL", "operator@example.com")
    monkeypatch.setenv("DEVLENS_JIRA_TOKEN", "jira-token")
    monkeypatch.setenv("DEVLENS_GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("DEVLENS_REPOSITORIES", "org/repo")
    monkeypatch.setenv("DEVLENS_JIRA_PROJECTS", "DEV")

    store = JobStore(tmp_path / "jobs.sqlite")
    app.state.auth = AuthGate(None)
    app.state.guard = CapabilityGuard()
    app.state.audit = AuditLog()
    app.state.jobs = JobService(store, guard=app.state.guard)
    app.state.approvals = ApprovalService(tmp_path / "approvals.sqlite")
    app.state.knowledge = KnowledgeStore()
    app.state.onboarding = OnboardingStore()
    app.state.agent = StubAgent()
    app.state.public_settings = Settings.from_env()
    app.state.config_error = None
    app.state.hosted = False
    return TestClient(app)


def completed_job(client, **overrides):
    store = app.state.jobs.store
    record = store.create(
        JobCreate(command="ticket", ticket_key="DEV-1", repository="org/repo"),
        run_id="run-1",
    )
    store.add_event(record.id, AgentEvent(ts=time.time(), kind="Step", message="m"))
    store.update(
        record.id,
        status="completed",
        result={
            "executive_summary": "checkout times out",
            "files_affected": ["src/checkout.py"],
            "ticket": {"key": "DEV-1", "summary": "Checkout"},
            "commit": "a" * 40,
            "evidence": [],
            **overrides,
        },
    )
    return record


# --------------------------------------------------------------------------- #
# Dashboard, integrations, services
# --------------------------------------------------------------------------- #


def test_the_dashboard_summarises_runs_and_capability_state(client):
    completed_job(client)
    body = client.get("/dashboard").json()
    assert body["counts"]["ticket"] == 1
    assert body["capabilities"]["denied"]
    assert body["integrations"]


def test_integrations_report_configuration_not_connection(client):
    items = client.get("/integrations").json()
    by_name = {item["name"]: item for item in items}
    assert by_name["jira"]["status"] == "healthy"
    assert by_name["loki"]["status"] == "not_configured"
    assert all(
        "environment variables" in item["configured_by"] for item in items
    ), "DevLens has no connect flow and must not imply one"
    assert "github-token" not in str(items)


def test_the_service_catalog_is_declared_and_addressable(client):
    items = client.get("/services").json()
    assert items, "the repository allowlist alone should populate the catalog"
    first = client.get(f"/services/{items[0]['id']}")
    assert first.status_code == 200
    assert client.get("/services/absent").status_code == 404


def test_projects_are_listed_without_their_credentials(client):
    items = client.get("/projects").json()
    assert items[0]["repositories"] == ["org/repo"]
    assert "token" not in str(items).lower()


# --------------------------------------------------------------------------- #
# Search, notifications, intent
# --------------------------------------------------------------------------- #


def test_search_spans_runs_knowledge_and_services(client):
    completed_job(client)
    client.post("/knowledge", json={"title": "Checkout runbook", "category": "runbook",
                                    "body": "steps"})
    groups = client.get("/search", params={"q": "checkout"}).json()["groups"]
    assert groups["runs"]
    assert groups["knowledge"]
    assert groups["code"]


def test_search_without_a_query_returns_empty_groups(client):
    groups = client.get("/search").json()["groups"]
    assert all(items == [] for items in groups.values())


def test_notifications_list_pending_work(client):
    completed_job(client)
    items = client.get("/notifications").json()
    assert any(item["kind"] == "run.completed" for item in items)


def test_intent_detection_is_exposed_for_the_omnibox(client):
    body = client.post("/intent", json={"text": "review PR 42 in org/repo"}).json()
    assert body["intent"] == "CODE_REVIEW"
    override = client.post(
        "/intent", json={"text": "anything", "override": "SYSTEM_DESIGN"}
    ).json()
    assert override["intent"] == "SYSTEM_DESIGN"


# --------------------------------------------------------------------------- #
# Knowledge and onboarding
# --------------------------------------------------------------------------- #


def test_knowledge_documents_round_trip_over_http(client):
    created = client.post(
        "/knowledge", json={"title": "Runbook", "category": "runbook", "body": "steps"}
    )
    assert created.status_code == 201
    document = created.json()
    assert client.get(f"/knowledge/{document['id']}").json()["title"] == "Runbook"
    assert client.get("/knowledge", params={"category": "runbook"}).json()
    assert client.get("/knowledge/absent").status_code == 404


def test_onboarding_is_derived_from_actual_state_not_a_stored_claim(client):
    """Configuration marks its own steps done; the optional one stays optional."""
    before = client.get("/onboarding").json()
    done = {step["id"]: step["done"] for step in before["steps"]}
    assert done["connect_jira"] is True, "settings are configured in this fixture"
    assert done["optional_observability"] is False
    assert before["complete"] is False

    marked = client.post("/onboarding", json={"step": "optional_observability"})
    assert marked.status_code == 200
    after = client.get("/onboarding").json()
    assert {step["id"]: step["done"] for step in after["steps"]}[
        "optional_observability"
    ] is True


def test_an_unknown_onboarding_step_is_ignored_rather_than_recorded(client):
    before = client.get("/onboarding").json()
    client.post("/onboarding", json={"step": "not-a-step"})
    assert client.get("/onboarding").json() == before


# --------------------------------------------------------------------------- #
# Capacity and settings sections
# --------------------------------------------------------------------------- #


def test_capacity_is_computed_from_the_supplied_inputs(client):
    body = client.get(
        "/capacity", params={"events_per_day": 86_400, "peak_multiplier": 4,
                             "payload_kb": 10}
    ).json()
    assert body["average_per_sec"] == 1.0
    assert body["peak_per_sec"] == 4.0
    assert "no model is involved" in body["method"]


def test_every_settings_section_is_reachable(client):
    from devlens.app.catalog import SETTINGS_SECTIONS

    for name in SETTINGS_SECTIONS:
        response = client.get(f"/settings/{name}")
        assert response.status_code == 200, name
        assert response.json()["section"] == name
    assert client.get("/settings/nope").status_code == 404


def test_the_audit_endpoint_returns_redacted_events(client):
    app.state.audit.record({"event": "x", "github_token": "ghp_" + "a" * 36})
    body = client.get("/audit").json()
    assert body["events"][0]["github_token"] == "***"


# --------------------------------------------------------------------------- #
# Synchronous analysis routes
# --------------------------------------------------------------------------- #


def test_a_synchronous_ticket_analysis_returns_the_result(client):
    response = client.post(
        "/analyses/ticket", json={"ticket_key": "DEV-1", "repository": "org/repo"}
    )
    assert response.status_code == 200
    assert response.json()["ticket"]["key"] == "DEV-1"


def test_a_synchronous_platform_route_runs_without_credentials(client):
    response = client.post("/analyses/ask", json={"question": "what is idempotency?"})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_a_synchronous_route_without_an_agent_is_503(client):
    app.state.agent = None
    app.state.config_error = "DEVLENS_JIRA_URL is not set"
    response = client.post(
        "/analyses/ticket", json={"ticket_key": "DEV-1", "repository": "org/repo"}
    )
    assert response.status_code == 503


def test_an_analysis_of_a_path_outside_the_root_is_refused(client):
    response = client.post("/analyses/analyze", json={"path": "/etc"})
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def test_a_run_exports_as_markdown_with_a_filename(client):
    record = completed_job(client)
    response = client.get(f"/jobs/{record.id}/export", params={"format": "md"})
    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert f"devlens-{record.id}.md" in response.headers["content-disposition"]
    assert "DevLens" in response.text


def test_a_run_exports_as_json_by_default(client):
    record = completed_job(client)
    body = client.get(f"/jobs/{record.id}/export").json()
    assert body["executive_summary"] == "checkout times out"


def test_exporting_an_unfinished_run_is_refused(client):
    store = app.state.jobs.store
    record = store.create(JobCreate(command="ask", question="x"), run_id="run-2")
    response = client.get(f"/jobs/{record.id}/export")
    assert response.status_code in {404, 409}


def test_proposals_for_a_run_are_listed(client):
    response = client.post(
        "/proposals",
        json={
            "action": "post_jira_comment",
            "run_id": "run-1",
            "ticket_key": "DEV-1",
            "target": "DEV-1",
            "payload": {"body": "findings"},
            "rationale": "publish",
        },
    )
    assert response.status_code == 201
    listed = client.get("/proposals/run-1").json()
    assert [item["id"] for item in listed] == [response.json()["id"]]


def test_the_application_module_exposes_one_app(client):
    assert application is app
