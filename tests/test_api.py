"""HTTP surface.

Covers what a browser can actually do to DevLens: authenticate, be rate
limited, be refused cross-origin, be told a useful status code rather than 500,
resume a server-sent event stream from a cursor, and never receive a secret.

Regression coverage for DL-P1-012 (error taxonomy at the HTTP boundary),
DL-P1-015 (SSE replayed the whole timeline on reconnect), DL-P2-018 (no CSRF
protection on state-changing routes), DL-P2-019 (no request body ceiling) and
DL-P2-020 (settings echoed configuration back to the browser).
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from devlens.agent.audit import AuditLog
from devlens.app import api
from devlens.app.api import SECURITY_HEADERS, app, mount_ui, ui_directory
from devlens.app.approvals import ApprovalService
from devlens.app.auth import AuthGate
from devlens.app.jobs import JobService, JobStore
from devlens.domain import AgentEvent
from devlens.guardrails import CapabilityGuard


class StubAgent:
    async def ask(self, payload, context=None):
        return {"answer": "deterministic"}

    analyze = investigate = observe = design = ask

    def gateway_for_repository(self, repository, ticket_key):
        """No write-back attached: approving records the decision only."""
        return None


def wire(monkeypatch, tmp_path, *, password=None, guard=None, agent=None):
    """Assemble the application state the lifespan would normally build."""
    store = JobStore(tmp_path / "jobs.sqlite")
    app.state.auth = AuthGate(password)
    app.state.guard = guard or CapabilityGuard()
    app.state.audit = AuditLog()
    app.state.jobs = JobService(store, guard=app.state.guard, config_digest="d")
    app.state.approvals = ApprovalService(tmp_path / "approvals.sqlite")
    app.state.agent = agent if agent is not None else StubAgent()
    app.state.public_settings = None
    app.state.config_error = None
    app.state.hosted = False
    return store


@pytest.fixture
def client(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path)
    yield TestClient(app)


# --------------------------------------------------------------------------- #
# Health, headers, capabilities
# --------------------------------------------------------------------------- #


def test_health_does_not_require_a_session(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_every_response_carries_the_security_headers(client):
    response = client.get("/health")
    for header, value in SECURITY_HEADERS.items():
        assert response.headers[header] == value
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_capabilities_report_what_is_off_and_how_to_turn_it_on(client):
    body = client.get("/capabilities").json()
    assert body["enabled"] == []
    assert "git_writeback" in body["denied"]
    assert body["enable_with"]["git_writeback"] == "DEVLENS_ALLOW_WRITES=1"
    assert "DEVLENS_GITHUB_TOKEN" not in json.dumps(body)


def test_settings_never_echo_a_credential(monkeypatch, tmp_path):
    """DL-P2-020."""
    monkeypatch.setenv("DEVLENS_GITHUB_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.setenv("DEVLENS_JIRA_TOKEN", "jira-secret")
    wire(monkeypatch, tmp_path)
    test_client = TestClient(app)
    body = test_client.get("/settings").text
    assert "ghp_" not in body
    assert "jira-secret" not in body


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


def test_a_protected_route_requires_a_session(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path, password="correct-horse-battery")
    test_client = TestClient(app)
    assert test_client.get("/jobs").status_code == 401
    login = test_client.post("/auth/login", json={"password": "correct-horse-battery"})
    assert login.status_code == 200
    assert test_client.get("/jobs").status_code == 200


def test_the_session_cookie_is_httponly_and_samesite(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path, password="correct-horse-battery")
    test_client = TestClient(app)
    response = test_client.post(
        "/auth/login", json={"password": "correct-horse-battery"}
    )
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=strict" in header or "samesite=lax" in header


def test_a_wrong_password_is_refused_without_saying_why(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path, password="correct-horse-battery")
    test_client = TestClient(app)
    response = test_client.post("/auth/login", json={"password": "wrong"})
    assert response.status_code == 403
    assert "correct-horse" not in response.text


def test_logout_ends_the_session(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path, password="correct-horse-battery")
    test_client = TestClient(app)
    test_client.post("/auth/login", json={"password": "correct-horse-battery"})
    test_client.post("/auth/logout")
    assert test_client.get("/jobs").status_code == 401


# --------------------------------------------------------------------------- #
# Request guards
# --------------------------------------------------------------------------- #


def test_a_cross_origin_state_change_is_refused(client):
    """DL-P2-018: a cookie alone must not authorise a write from another site."""
    response = client.post(
        "/jobs",
        json={"command": "ask", "question": "hi"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["kind"] == "cross_origin"


def test_a_same_origin_state_change_is_allowed(client):
    response = client.post(
        "/jobs",
        json={"command": "ask", "question": "hi"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 202


def test_a_read_is_not_origin_checked(client):
    assert client.get("/jobs", headers={"origin": "https://evil.example"}).status_code == 200


def test_an_oversized_body_is_refused_before_it_is_parsed(client):
    """DL-P2-019."""
    response = client.post(
        "/jobs",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": str(5 * 1024 * 1024)},
    )
    assert response.status_code == 413
    assert response.json()["kind"] == "payload_too_large"


# --------------------------------------------------------------------------- #
# Jobs over HTTP
# --------------------------------------------------------------------------- #


def test_a_job_runs_and_its_result_is_retrievable(client):
    created = client.post("/jobs", json={"command": "ask", "question": "why?"}).json()
    for _ in range(200):
        record = client.get(f"/jobs/{created['id']}").json()
        if record["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert record["status"] == "completed"
    assert record["result"]["answer"] == "deterministic"
    assert client.get(f"/jobs/{created['id']}/run").json()["config_digest"] == "d"


def test_an_invalid_request_is_422_not_500(client):
    response = client.post("/jobs", json={"command": "not-a-command"})
    assert response.status_code == 422


def test_a_missing_job_is_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/events").status_code == 404


def test_an_analyze_path_outside_the_root_is_403_with_a_reason(client):
    response = client.post("/jobs", json={"command": "analyze", "path": "/etc"})
    assert response.status_code == 403
    assert "workspace root" in response.json()["detail"]


def test_capacity_is_reported_as_429_with_a_retry_header(monkeypatch, tmp_path):
    """A full queue is a documented answer with a Retry-After, not a 500."""
    from devlens.domain import CapacityReached

    wire(monkeypatch, tmp_path)

    def refuse(self, agent, job):
        raise CapacityReached("32 jobs are already running; retry shortly.")

    monkeypatch.setattr(type(app.state.jobs), "submit", refuse)
    response = TestClient(app).post("/jobs", json={"command": "ask", "question": "a"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert "retry shortly" in response.json()["detail"]


def test_cancelling_a_finished_job_is_409(client):
    created = client.post("/jobs", json={"command": "ask", "question": "why?"}).json()
    for _ in range(200):
        if client.get(f"/jobs/{created['id']}").json()["status"] == "completed":
            break
        time.sleep(0.01)
    response = client.post(f"/jobs/{created['id']}/cancel")
    assert response.status_code == 409


def test_the_service_reports_503_when_it_is_not_configured(monkeypatch, tmp_path):
    wire(monkeypatch, tmp_path)
    app.state.agent = None
    app.state.config_error = "DEVLENS_JIRA_URL is not set"
    test_client = TestClient(app)
    response = test_client.get("/ready")
    assert response.status_code == 503
    assert "DEVLENS_JIRA_URL" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Server-sent events
# --------------------------------------------------------------------------- #


def test_the_event_stream_resumes_from_a_cursor(monkeypatch, tmp_path):
    """DL-P1-015: a reconnect must not replay the whole timeline."""
    store = wire(monkeypatch, tmp_path)
    record = store.create(
        api.JobCreate(command="ask", question="why?"), run_id="run-1"
    )
    for index in range(3):
        store.add_event(
            record.id, AgentEvent(ts=time.time(), kind="Step", message=f"m{index}")
        )
    store.update(record.id, status="completed")

    test_client = TestClient(app)
    first = test_client.get(f"/jobs/{record.id}/events").text
    assert "m0" in first and "m2" in first
    seq = int(first.rsplit("id: ", 1)[1].split("\n", 1)[0])
    resumed = test_client.get(
        f"/jobs/{record.id}/events", headers={"last-event-id": str(seq)}
    ).text
    assert "m0" not in resumed
    assert "RunFinished" in resumed


def test_the_stream_ends_when_the_job_ends(monkeypatch, tmp_path):
    store = wire(monkeypatch, tmp_path)
    record = store.create(api.JobCreate(command="ask", question="x"), run_id="run-1")
    store.update(record.id, status="failed", error_kind="timeout")
    test_client = TestClient(app)
    body = test_client.get(f"/jobs/{record.id}/events").text
    assert "RunFinished" in body
    assert '"error_kind": "timeout"' in body


# --------------------------------------------------------------------------- #
# Approvals over HTTP
# --------------------------------------------------------------------------- #


def test_the_approval_round_trip(client):
    created = client.post(
        "/proposals",
        json={
            "action": "post_pr_comment",
            "run_id": "run-1",
            "repository": "org/repo",
            "target": "org/repo#7",
            "payload": {"number": 7, "body": "findings"},
            "rationale": "publish the review",
        },
    )
    assert created.status_code == 201
    proposal = created.json()
    assert len(proposal["payload_sha256"]) == 64

    approval = client.post("/approvals", json={"proposal_id": proposal["id"]}).json()
    assert approval["status"] == "pending"
    assert client.get("/approvals").json()[0]["id"] == approval["id"]

    decided = client.post(f"/approvals/{approval['id']}/approve").json()
    assert decided["status"] == "approved"
    assert "Write-back is disabled" in decided["result"]

    again = client.post(f"/approvals/{approval['id']}/approve")
    assert again.status_code == 409


def test_rejecting_an_unknown_approval_is_404(client):
    assert client.post("/approvals/nope/reject").status_code == 404


# --------------------------------------------------------------------------- #
# UI packaging
# --------------------------------------------------------------------------- #


def test_the_ui_directory_can_be_overridden(monkeypatch, tmp_path):
    """The container image sets this; without it the image served no UI."""
    monkeypatch.setenv("DEVLENS_UI_DIR", str(tmp_path))
    assert ui_directory() == tmp_path


def test_mounting_is_reported_rather_than_failing_silently(tmp_path):
    assert mount_ui(app, tmp_path / "absent") is False


def test_the_built_ui_is_served_when_it_exists(tmp_path):
    from fastapi import FastAPI

    (tmp_path / "index.html").write_text("<!doctype html><title>DevLens</title>")
    application = FastAPI()
    assert mount_ui(application, tmp_path) is True
    with TestClient(application) as test_client:
        assert "DevLens" in test_client.get("/").text


def test_the_probes_answer_head_as_well_as_get(client):
    """Uptime checks routinely use HEAD; a 404 there reads as an outage."""
    assert client.head("/health").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.head("/ready").status_code in {200, 503}
