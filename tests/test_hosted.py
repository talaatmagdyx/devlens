"""The hosted, multi-tenant front door.

The local application authenticates one operator on a loopback interface; this
one is what DevLens offers when it is reachable by more than the person who
started it. What has to hold: a tenant cannot see another tenant's runs, a
browser write needs a matching origin, an invalid bearer header never falls back
to a cookie, and the commands that touch a host path or execute repository code
are refused outright because no isolated runner exists yet.

Regression coverage for DL-P1-018 (the hosted mode shared one job store),
DL-P2-030 (tenants shared an audit log) and DL-P3-036 (the access log recorded
query strings).
"""

from __future__ import annotations

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from devlens.app.hosting import HostingConfig
from devlens.app.identity import (
    SESSION_COOKIE,
    BearerAuthentication,
    IdentityStore,
    Principal,
    authenticate,
)
from devlens.app.saas import HOSTED_COMMANDS, HostedPolicy, RequestQuota
from devlens.app.saas import app as hosted_app
from devlens.domain import AccessDenied

TOKEN_A = "a" * 40
TOKEN_B = "b" * 40


def config(tmp_path, **overrides) -> dict:
    return {
        "public_origin": "https://devlens.example",
        "data_dir": str(tmp_path / "data"),
        "tenants": {
            "acme": {"projects": ["acme"]},
            "globex": {"projects": ["globex"]},
        },
        "identities": {
            "alice": {"tenant": "acme", "role": "operator", "token_env": "TOKEN_A"},
            "bob": {"tenant": "globex", "role": "viewer", "token_env": "TOKEN_B"},
        },
        **overrides,
    }


@pytest.fixture
def hosting(tmp_path, monkeypatch) -> HostingConfig:
    monkeypatch.setenv("TOKEN_A", TOKEN_A)
    monkeypatch.setenv("TOKEN_B", TOKEN_B)
    return HostingConfig.model_validate(config(tmp_path))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_a_valid_hosting_configuration_is_accepted(hosting):
    assert set(hosting.tenants) == {"acme", "globex"}
    assert hosting.public_origin == "https://devlens.example"
    assert hosting.session_seconds == 3600


@pytest.mark.parametrize(
    "origin",
    [
        "http://devlens.example",
        "https://user:pw@devlens.example",
        "https://devlens.example/path",
        "https://devlens.example?x=1",
        "not-a-url",
    ],
)
def test_the_public_origin_must_be_an_https_origin(origin, tmp_path):
    with pytest.raises(ValidationError, match="HTTPS origin"):
        HostingConfig.model_validate(config(tmp_path, public_origin=origin))


def test_the_data_directory_must_be_absolute(tmp_path):
    with pytest.raises(ValidationError, match="must be absolute"):
        HostingConfig.model_validate(config(tmp_path, data_dir="relative/path"))


def test_a_project_may_belong_to_only_one_tenant(tmp_path):
    """DL-P1-018: shared state between tenants is the failure that matters."""
    with pytest.raises(ValidationError, match="exactly one tenant"):
        HostingConfig.model_validate(
            config(
                tmp_path,
                tenants={"acme": {"projects": ["shared"]},
                         "globex": {"projects": ["shared"]}},
            )
        )


def test_an_identity_must_refer_to_a_known_tenant(tmp_path):
    with pytest.raises(ValidationError, match="unknown tenant"):
        HostingConfig.model_validate(
            config(
                tmp_path,
                identities={"eve": {"tenant": "absent", "token_env": "TOKEN_A"}},
            )
        )


@pytest.mark.parametrize("name", ["has space", "../escape", "a" * 81, ""])
def test_a_tenant_name_that_is_not_a_safe_identifier_is_refused(name, tmp_path):
    with pytest.raises(ValidationError):
        HostingConfig.model_validate(
            config(tmp_path, tenants={name: {"projects": ["acme"]}})
        )


def test_an_unknown_hosting_key_is_refused(tmp_path):
    with pytest.raises(ValidationError):
        HostingConfig.model_validate(config(tmp_path, public_orgin="https://x.example"))


def test_hosting_configuration_is_required(monkeypatch):
    monkeypatch.delenv("DEVLENS_SAAS_CONFIG", raising=False)
    with pytest.raises(ValueError, match="DEVLENS_SAAS_CONFIG is required"):
        HostingConfig.from_env()


def test_hosting_configuration_loads_from_toml(tmp_path, monkeypatch):
    path = tmp_path / "hosting.toml"
    path.write_text(
        f"""
public_origin = "https://devlens.example"
data_dir = "{tmp_path / 'data'}"

[tenants.acme]
projects = ["acme"]

[identities.alice]
tenant = "acme"
role = "operator"
token_env = "TOKEN_A"
"""
    )
    monkeypatch.setenv("DEVLENS_SAAS_CONFIG", str(path))
    assert set(HostingConfig.from_env().tenants) == {"acme"}


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def test_a_short_or_padded_token_is_refused_at_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_A", "short")
    monkeypatch.setenv("TOKEN_B", TOKEN_B)
    with pytest.raises(ValueError, match="at least 32 ASCII characters"):
        IdentityStore(HostingConfig.model_validate(config(tmp_path)))

    monkeypatch.setenv("TOKEN_A", " " + TOKEN_A)
    with pytest.raises(ValueError):
        IdentityStore(HostingConfig.model_validate(config(tmp_path)))


def test_two_identities_may_not_share_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_A", TOKEN_A)
    monkeypatch.setenv("TOKEN_B", TOKEN_A)
    with pytest.raises(ValueError, match="must be unique"):
        IdentityStore(HostingConfig.model_validate(config(tmp_path)))


def test_a_bearer_token_resolves_to_its_principal(hosting):
    store = IdentityStore(hosting)
    assert store.bearer(TOKEN_A) == Principal("alice", "acme", "operator")
    assert store.bearer(TOKEN_B).tenant == "globex"
    assert store.bearer("wrong") is None


def test_a_session_expires_and_can_be_revoked(hosting):
    store = IdentityStore(hosting)
    store.ttl = 0
    token = store.issue(store.bearer(TOKEN_A))
    assert store.session(token) is None

    store.ttl = 3600
    live = store.issue(store.bearer(TOKEN_A))
    assert store.session(live).subject == "alice"
    store.revoke(live)
    assert store.session(live) is None


def test_sessions_are_capped(hosting):
    store = IdentityStore(hosting)
    principal = store.bearer(TOKEN_A)
    for _ in range(1000):
        store.issue(principal)
    with pytest.raises(ValueError, match="capacity"):
        store.issue(principal)


def test_an_invalid_authorization_header_never_falls_back_to_a_cookie(hosting):
    """Otherwise a stolen cookie is reachable by an attacker who sends garbage."""
    store = IdentityStore(hosting)
    session = store.issue(store.bearer(TOKEN_A))

    def request(headers, cookies=""):
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/jobs",
                "headers": [
                    *[(k.encode(), v.encode()) for k, v in headers.items()],
                    *([(b"cookie", cookies.encode())] if cookies else []),
                ],
            }
        )

    cookie = f"{SESSION_COOKIE}={session}"
    assert authenticate(request({}, cookie), store).subject == "alice"
    assert authenticate(request({"authorization": "Bearer wrong"}, cookie), store) is None
    assert authenticate(request({"authorization": "Basic x"}, cookie), store) is None
    assert authenticate(request({"authorization": f"Bearer {TOKEN_A}"}), store).subject == (
        "alice"
    )


def test_an_oversized_bearer_token_is_rejected_without_a_lookup(hosting):
    store = IdentityStore(hosting)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/jobs",
            "headers": [(b"authorization", b"Bearer " + b"x" * 5000)],
        }
    )
    assert BearerAuthentication().authenticate(request, store) is None


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def operator() -> Principal:
    return Principal("alice", "acme", "operator")


def viewer() -> Principal:
    return Principal("bob", "globex", "viewer")


def test_a_viewer_may_read_but_not_write():
    HostedPolicy.authorize(viewer(), "/jobs", "GET", {}, {"globex"})
    with pytest.raises(AccessDenied, match="Operator role required"):
        HostedPolicy.authorize(viewer(), "/jobs", "POST", {"command": "ask"}, {"globex"})


def test_hosted_remote_writes_are_disabled_entirely():
    for path in ("/approvals", "/approvals/x/approve", "/proposals"):
        with pytest.raises(AccessDenied, match="Hosted remote writes are disabled"):
            HostedPolicy.authorize(operator(), path, "POST", {}, {"acme"})


@pytest.mark.parametrize("command", sorted(HOSTED_COMMANDS))
def test_every_hosted_command_is_permitted_for_its_own_project(command):
    HostedPolicy.authorize(
        operator(), "/jobs", "POST", {"command": command, "project": "acme"}, {"acme"}
    )


@pytest.mark.parametrize("command", ["analyze", "implement"])
def test_commands_that_need_a_host_path_or_code_execution_are_refused(command):
    with pytest.raises(AccessDenied, match="isolated runner"):
        HostedPolicy.authorize(
            operator(), "/jobs", "POST", {"command": command}, {"acme"}
        )


def test_a_path_or_a_test_run_is_refused_even_for_a_permitted_command():
    with pytest.raises(AccessDenied, match="isolated runner"):
        HostedPolicy.authorize(
            operator(), "/jobs", "POST", {"command": "ask", "path": "/etc"}, {"acme"}
        )
    with pytest.raises(AccessDenied, match="isolated runner"):
        HostedPolicy.authorize(
            operator(),
            "/jobs",
            "POST",
            {"command": "review", "run_tests": True},
            {"acme"},
        )


def test_a_tenant_cannot_reach_another_tenants_project():
    with pytest.raises(AccessDenied, match="not available to this tenant"):
        HostedPolicy.authorize(
            operator(), "/jobs", "POST", {"command": "ask", "project": "globex"},
            {"acme"},
        )


def test_the_analyses_route_is_policed_the_same_way():
    HostedPolicy.authorize(operator(), "/analyses/ask", "POST", {}, {"acme", "default"})
    with pytest.raises(AccessDenied, match="isolated runner"):
        HostedPolicy.authorize(
            operator(), "/analyses/implement", "POST", {}, {"acme", "default"}
        )


# --------------------------------------------------------------------------- #
# Quota
# --------------------------------------------------------------------------- #


def test_the_request_quota_is_a_fixed_window():
    quota = RequestQuota(limit=2, seconds=60)
    assert quota.allow() is True
    assert quota.allow() is True
    assert quota.allow() is False


def test_the_window_rolls_forward():
    quota = RequestQuota(limit=1, seconds=0.01)
    assert quota.allow() is True
    time.sleep(0.02)
    assert quota.allow() is True


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #


@pytest.fixture
def hosted_config(tmp_path, monkeypatch, single_project):
    path = tmp_path / "hosting.toml"
    path.write_text(
        f"""
public_origin = "https://devlens.example"
data_dir = "{tmp_path / 'data'}"

[tenants.acme]
projects = ["default"]

[identities.alice]
tenant = "acme"
role = "operator"
token_env = "TOKEN_A"
"""
    )
    monkeypatch.setenv("DEVLENS_SAAS_CONFIG", str(path))
    monkeypatch.setenv("TOKEN_A", TOKEN_A)
    return path


@pytest.fixture
def hosted(hosted_config):
    with TestClient(hosted_app, base_url="https://devlens.example") as client:
        yield client


def test_health_is_public_and_says_nothing_about_tenants(hosted):
    response = hosted.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_security_headers_are_present_on_every_response(hosted):
    headers = hosted.get("/health").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"
    assert headers["strict-transport-security"].startswith("max-age=")
    assert headers["x-request-id"]


def test_an_unauthenticated_api_call_is_401_with_a_challenge(hosted):
    response = hosted.get("/jobs")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_bearer_token_reaches_the_tenant_application(hosted):
    response = hosted.get("/jobs", headers={"authorization": f"Bearer {TOKEN_A}"})
    assert response.status_code == 200
    assert response.json() == []


def test_a_browser_write_from_another_origin_is_refused(hosted):
    response = hosted.post(
        "/jobs",
        json={"command": "ask", "question": "why?"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "Origin" in response.json()["detail"]


def test_plain_http_is_refused(hosted_config):
    """A bearer token must never travel in clear text."""
    with TestClient(hosted_app, base_url="http://devlens.example") as client:
        response = client.get("/jobs", headers={"authorization": f"Bearer {TOKEN_A}"})
    assert response.status_code == 400
    assert "HTTPS is required" in response.json()["detail"]


def test_login_issues_a_host_prefixed_secure_cookie(hosted):
    response = hosted.post(
        "/auth/login",
        json={"password": TOKEN_A},
        headers={"origin": "https://devlens.example"},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert SESSION_COOKIE in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_a_wrong_password_is_401(hosted):
    response = hosted.post(
        "/auth/login",
        json={"password": "wrong"},
        headers={"origin": "https://devlens.example"},
    )
    assert response.status_code == 401


def test_an_oversized_body_is_refused(hosted):
    response = hosted.post(
        "/jobs",
        content=b"{}" + b" " * 70_000,
        headers={
            "authorization": f"Bearer {TOKEN_A}",
            "content-type": "application/json",
            "origin": "https://devlens.example",
        },
    )
    assert response.status_code == 413


def test_invalid_json_is_400(hosted):
    response = hosted.post(
        "/jobs",
        content=b"{not json",
        headers={
            "authorization": f"Bearer {TOKEN_A}",
            "content-type": "application/json",
            "origin": "https://devlens.example",
        },
    )
    assert response.status_code == 400


def test_a_host_path_job_is_refused_by_the_gateway(hosted):
    response = hosted.post(
        "/jobs",
        json={"command": "analyze", "path": "/etc"},
        headers={
            "authorization": f"Bearer {TOKEN_A}",
            "origin": "https://devlens.example",
        },
    )
    assert response.status_code == 403
    assert "isolated runner" in response.json()["detail"]


def test_the_access_log_records_no_credentials_or_query_strings(hosted, caplog):
    """DL-P3-036."""
    with caplog.at_level(logging.INFO, logger="devlens.http"):
        hosted.get(
            "/search?q=secret-query",
            headers={"authorization": f"Bearer {TOKEN_A}"},
        )
    entries = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "devlens.http"
    ]
    assert entries
    entry = entries[-1]
    assert entry["tenant"] == "acme"
    assert entry["subject"] == "alice"
    assert entry["path"] == "/search"
    assert "secret-query" not in json.dumps(entry)
    assert TOKEN_A not in json.dumps(entry)
    assert set(entry) == {
        "request_id", "tenant", "subject", "method", "path", "status", "duration_ms"
    }


# --------------------------------------------------------------------------- #
# Isolation between tenants, proven through the gateway
# --------------------------------------------------------------------------- #


@pytest.fixture
def two_tenants(tmp_path, monkeypatch):
    """Two tenants, two identities, two projects — the real hosted wiring."""
    for name, project in (("acme", "ACME"), ("globex", "GLOBEX")):
        monkeypatch.setenv(f"{project}_JIRA_TOKEN", f"{name}-jira-token")
        monkeypatch.setenv(f"{project}_GITHUB_TOKEN", f"{name}-github-token")
    monkeypatch.setenv("TOKEN_A", TOKEN_A)
    monkeypatch.setenv("TOKEN_B", TOKEN_B)

    settings = tmp_path / "devlens.toml"
    settings.write_text(
        """
[projects.acme]
jira_url = "https://acme.atlassian.net"
jira_email = "ops@acme.example"
jira_token_env = "ACME_JIRA_TOKEN"
git_token_env = "ACME_GITHUB_TOKEN"
repositories = ["acme/checkout"]
jira_projects = ["ACME"]

[projects.globex]
jira_url = "https://globex.atlassian.net"
jira_email = "ops@globex.example"
jira_token_env = "GLOBEX_JIRA_TOKEN"
git_token_env = "GLOBEX_GITHUB_TOKEN"
repositories = ["globex/ledger"]
jira_projects = ["GLOB"]
"""
    )
    hosting = tmp_path / "hosting.toml"
    hosting.write_text(
        f"""
public_origin = "https://devlens.example"
data_dir = "{tmp_path / 'data'}"

[tenants.acme]
projects = ["acme"]

[tenants.globex]
projects = ["globex"]

[identities.alice]
tenant = "acme"
role = "operator"
token_env = "TOKEN_A"

[identities.bob]
tenant = "globex"
role = "operator"
token_env = "TOKEN_B"
"""
    )
    monkeypatch.setenv("DEVLENS_CONFIG", str(settings))
    monkeypatch.setenv("DEVLENS_SAAS_CONFIG", str(hosting))
    return tmp_path


def as_alice() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN_A}", "origin": "https://devlens.example"}


def as_bob() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN_B}", "origin": "https://devlens.example"}


def test_one_tenant_cannot_see_another_tenants_run(two_tenants):
    """DL-P1-018, proven through the gateway rather than asserted at load."""
    with TestClient(hosted_app, base_url="https://devlens.example") as client:
        created = client.post(
            "/jobs",
            json={"command": "ask", "question": "why is checkout slow?", "project": "acme"},
            headers=as_alice(),
        )
        assert created.status_code == 202
        job = created.json()["id"]

        assert [item["id"] for item in client.get("/jobs", headers=as_alice()).json()] == [job]

        # Bob is an operator too — of a different tenant.
        assert client.get("/jobs", headers=as_bob()).json() == []
        assert client.get(f"/jobs/{job}", headers=as_bob()).status_code == 404
        assert client.get(f"/jobs/{job}/events", headers=as_bob()).status_code == 404
        assert client.get(f"/jobs/{job}/export", headers=as_bob()).status_code == 404


def test_each_tenant_gets_its_own_stores_on_disk(two_tenants):
    with TestClient(hosted_app, base_url="https://devlens.example"):
        root = two_tenants / "data"
        assert sorted(item.name for item in root.iterdir() if item.is_dir()) == [
            "acme",
            "globex",
        ]
        # 0700: another local account cannot read a tenant's job store.
        assert oct(root.stat().st_mode)[-3:] == "700"
        assert oct((root / "acme").stat().st_mode)[-3:] == "700"


def test_a_tenant_cannot_reach_a_project_it_does_not_own(two_tenants):
    with TestClient(hosted_app, base_url="https://devlens.example") as client:
        response = client.post(
            "/jobs",
            json={"command": "ask", "question": "why?", "project": "globex"},
            headers=as_alice(),
        )
        assert response.status_code == 403
        assert "not available to this tenant" in response.json()["detail"]


def test_each_tenant_writes_to_its_own_audit_log(two_tenants):
    with TestClient(hosted_app, base_url="https://devlens.example") as client:
        client.post(
            "/jobs",
            json={"command": "ask", "question": "why?", "project": "acme"},
            headers=as_alice(),
        )
        acme = (two_tenants / "data" / "acme" / "audit.jsonl")
        globex = (two_tenants / "data" / "globex" / "audit.jsonl")
        assert not globex.exists() or globex.read_text() == ""
        assert not acme.exists() or "globex" not in acme.read_text()


def test_a_second_worker_on_the_same_data_directory_fails_fast(two_tenants):
    """SQLite plus one worker is a deliberate bound, and it is enforced.

    Two workers against one data directory would corrupt nothing but would
    duplicate in-flight runs and split the approval queue, so the second refuses
    to start rather than starting wrong.
    """
    with TestClient(hosted_app, base_url="https://devlens.example") as first:
        assert first.get("/health").status_code == 200

        with (
            pytest.raises(ValueError, match="one worker per data directory"),
            TestClient(hosted_app, base_url="https://devlens.example"),
        ):
            pass  # pragma: no cover - the lifespan raises before this runs


def test_the_lock_is_released_when_the_worker_stops(two_tenants):
    with TestClient(hosted_app, base_url="https://devlens.example") as client:
        assert client.get("/health").status_code == 200
    # A restart succeeds; the bound is on concurrency, not on ever starting again.
    with TestClient(hosted_app, base_url="https://devlens.example") as client:
        assert client.get("/health").status_code == 200
