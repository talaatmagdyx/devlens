"""Provider adapters and the resilience layer.

Providers are where DevLens meets systems it does not control, so the tests
here are about what happens when those systems answer badly: a truncated tree,
a malformed payload, a paginated ticket, a rate limit, a flapping host.

Regression coverage for DL-P1-014 (only the first page of Jira comments was
read, silently), DL-P2-015 (a truncated GitHub tree produced a confident
partial analysis) and DL-P2-024 (one failing provider opened the circuit for
all of them).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from devlens.domain import ProviderError, ProviderResult
from devlens.providers.git.github import GitHubProvider
from devlens.providers.jira.cloud import JiraCloudProvider, adf_text
from devlens.providers.observe import ObserveHub
from devlens.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    PolicyRegistry,
    RateLimiter,
    ResiliencePolicy,
)
from tests.conftest import SHA, github_transport, jira_transport


def github(**kwargs) -> GitHubProvider:
    return GitHubProvider(
        httpx.AsyncClient(
            base_url="https://api.github.com/", transport=github_transport(**kwargs)
        )
    )


def jira(**kwargs) -> JiraCloudProvider:
    return JiraCloudProvider(
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/", transport=jira_transport(**kwargs)
        )
    )


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_truncated_tree_is_refused_rather_than_analysed_partially():
    """DL-P2-015: a partial file list yields confidently wrong conclusions."""

    def handler(request):
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"truncated": True, "tree": []})
        return httpx.Response(404, json={})

    provider = GitHubProvider(
        httpx.AsyncClient(
            base_url="https://api.github.com/", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(ProviderError, match="partial analysis would be misleading"):
        await provider.list_files("org/repo", SHA)


@pytest.mark.asyncio
async def test_only_regular_blobs_are_listed():
    files = await github(
        files=[
            {"path": "a.py", "type": "blob", "mode": "100644", "size": 10},
            {"path": "bin", "type": "blob", "mode": "120000", "size": 10},
            {"path": "sub", "type": "tree", "mode": "040000"},
        ]
    ).list_files("org/repo", SHA)
    assert [item.path for item in files] == ["a.py"]


@pytest.mark.asyncio
async def test_an_evidence_url_pins_a_commit_not_a_branch():
    url = github().evidence_url("org/repo", SHA, "src/a b.py", 12)
    assert f"/blob/{SHA}/" in url
    assert "%20" in url or "a%20b" in url
    assert url.endswith("#L12")


@pytest.mark.asyncio
async def test_a_commit_that_is_not_a_sha_is_refused():
    def handler(request):
        return httpx.Response(200, json={"sha": "not-a-sha"})

    provider = GitHubProvider(
        httpx.AsyncClient(
            base_url="https://api.github.com/", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(ProviderError, match="invalid commit"):
        await provider.resolve_ref("org/repo", "main")


@pytest.mark.asyncio
async def test_a_binary_file_is_refused_rather_than_mangled():
    provider = github(content=b"\x00\x01binary")
    with pytest.raises(ProviderError, match="oversized, binary, or malformed"):
        await provider.read_file("org/repo", SHA, "a.bin")


@pytest.mark.asyncio
async def test_pull_request_files_are_paginated_and_the_ceiling_is_reported():
    """A 400-file pull request must not be reviewed as if it were 100 files."""
    from devlens.providers.git import github as module

    files, more = await github(pr_pages=1).get_pull_request_files("org/repo", 7)
    assert len(files) == 1 and more is False

    files, more = await github(pr_pages=module.MAX_PAGES + 5).get_pull_request_files(
        "org/repo", 7
    )
    assert len(files) == module.MAX_PAGES
    assert more is True, "the caller must be able to record the truncation"


@pytest.mark.asyncio
async def test_a_default_branch_is_read_from_the_repository_not_assumed():
    provider = github()
    assert await provider.default_branch("org/repo") == "trunk"
    # Cached: a second call does not re-query.
    assert await provider.default_branch("org/repo") == "trunk"


# --------------------------------------------------------------------------- #
# Jira
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_comment_page_is_read():
    """DL-P1-014: the embedded field returns one page and says nothing about it."""
    context = await jira(comments=250).get_issue_context("DEV-1")
    assert len(context.comments) == 250
    assert context.truncations == []


@pytest.mark.asyncio
async def test_comments_beyond_the_cap_are_recorded_as_a_truncation():
    context = await jira(comments=40, total=900).get_issue_context("DEV-1")
    truncation = context.truncations[0]
    assert truncation.total == 900
    assert truncation.fetched == 40
    assert "capped" in truncation.reason


@pytest.mark.asyncio
async def test_a_malformed_issue_is_a_provider_error_not_a_crash():
    def handler(request):
        return httpx.Response(200, json={"fields": None})

    provider = JiraCloudProvider(
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(ProviderError, match="malformed ticket data"):
        await provider.get_ticket("DEV-1")


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"type": "text", "text": "plain"}, "plain"),
        ({"type": "hardBreak"}, "\n"),
        ({"type": "mention", "attrs": {"text": "@dev"}}, "@dev"),
        ({"type": "inlineCard", "attrs": {"url": "https://x/y"}}, "https://x/y"),
        ("bare string", "bare string"),
        (None, ""),
        (42, ""),
    ],
)
def test_atlassian_document_format_is_flattened_defensively(node, expected):
    assert adf_text(node) == expected


@pytest.mark.asyncio
async def test_a_ticket_records_when_it_was_read():
    before = time.time()
    ticket = await jira().get_ticket("DEV-1")
    assert before <= ticket.snapshot_at <= time.time()
    assert ticket.url.endswith("/browse/DEV-1")


# --------------------------------------------------------------------------- #
# Observability hub
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_unconfigured_provider_reports_not_configured():
    hub = ObserveHub()
    results = await hub.collect("checkout")
    assert set(results) == {"log", "metric", "trace", "sql"}
    assert {item.status for item in results.values()} == {"NOT_CONFIGURED"}
    assert all(item.rows == [] for item in results.values())


@pytest.mark.asyncio
async def test_a_provider_that_answers_nothing_is_empty_not_unavailable(monkeypatch):
    monkeypatch.setenv("DEVLENS_LOKI_URL", "https://logs.internal/")
    hub = ObserveHub.from_env()
    hub.loki = httpx.AsyncClient(
        base_url="https://logs.internal/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"result": []}})
        ),
    )
    result = await hub.search_logs("checkout")
    assert result.status == "EMPTY"
    assert result.rows == []


@pytest.mark.asyncio
async def test_a_provider_that_cannot_be_reached_is_unavailable(monkeypatch):
    monkeypatch.setenv("DEVLENS_PROM_URL", "https://metrics.internal/")
    hub = ObserveHub.from_env()
    hub.prometheus = httpx.AsyncClient(
        base_url="https://metrics.internal/",
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))
        ),
    )
    result = await hub.query_metrics("rate(x[5m])")
    assert result.status == "UNAVAILABLE"
    assert result.error


def test_a_provider_result_carries_the_query_that_produced_it():
    result = ProviderResult(
        provider="Loki", status="AVAILABLE", rows=["line"], query='{job="checkout"}'
    )
    assert result.query == '{job="checkout"}'


# --------------------------------------------------------------------------- #
# Resilience
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_circuit_opens_after_repeated_failures_and_recovers():
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=0.05)

    async def failing():
        raise ProviderError("down")

    for _ in range(2):
        with pytest.raises(ProviderError):
            await breaker.call(failing)
    with pytest.raises(CircuitOpenError):
        await breaker.call(failing)

    await asyncio.sleep(0.06)

    async def working():
        return "ok"

    assert await breaker.call(working) == "ok"


@pytest.mark.asyncio
async def test_each_host_gets_its_own_circuit():
    """DL-P2-024: Jira being down must not stop DevLens reading GitHub."""
    registry = PolicyRegistry(rate=100, burst=100, threshold=1, recovery=60.0, timeout=5)
    jira_policy = registry.for_host("https://team.atlassian.net")
    github_policy = registry.for_host("https://api.github.com")
    assert jira_policy is not github_policy
    assert registry.for_host("https://team.atlassian.net/x") is jira_policy

    async def failing():
        raise ProviderError("down")

    with pytest.raises(ProviderError):
        await jira_policy.call(failing)
    with pytest.raises(CircuitOpenError):
        await jira_policy.call(failing)

    async def working():
        return "ok"

    assert await github_policy.call(working) == "ok"


@pytest.mark.asyncio
async def test_the_rate_limiter_admits_the_burst_then_paces():
    limiter = RateLimiter(rate=50, burst=2)
    started = time.monotonic()
    for _ in range(4):
        await limiter.acquire()
    assert time.monotonic() - started >= 0.02


@pytest.mark.asyncio
async def test_a_policy_enforces_its_own_timeout():
    policy = ResiliencePolicy(RateLimiter(100, 100), CircuitBreaker(5, 1.0), timeout=0.05)

    async def slow():
        await asyncio.sleep(5)

    with pytest.raises(ProviderError):
        await policy.call(slow)


# --------------------------------------------------------------------------- #
# Atlassian Document Format, and the fields a real ticket carries
# --------------------------------------------------------------------------- #


def adf(*content) -> dict:
    return {"type": "doc", "version": 1, "content": list(content)}


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"type": "emoji", "attrs": {"shortName": ":warning:"}}, ":warning:"),
        ({"type": "codeBlock", "content": [{"type": "text", "text": "x = 1"}]}, "\nx = 1\n"),
        (
            {"type": "media", "attrs": {"id": "abc"}},
            "[attachment abc]",
        ),
        ({"type": "heading", "content": [{"type": "text", "text": "Steps"}]}, "Steps\n"),
        (
            {"type": "blockquote", "content": [{"type": "text", "text": "quoted"}]},
            "quoted\n",
        ),
        (
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "text", "text": "one"}]}
                ],
            },
            "one\n",
        ),
    ],
)
def test_every_document_node_flattens_to_readable_text(node, expected):
    assert adf_text(node) == expected


def test_a_table_flattens_row_by_row():
    table = {
        "type": "table",
        "content": [
            {
                "type": "tableRow",
                "content": [
                    {"type": "tableCell", "content": [{"type": "text", "text": "a"}]},
                    {"type": "tableCell", "content": [{"type": "text", "text": "b"}]},
                ],
            }
        ],
    }
    assert adf_text(table) == "a\tb\t\n\n"


@pytest.mark.asyncio
async def test_an_issue_carries_the_fields_an_analysis_depends_on():
    def handler(request):
        path = request.url.path
        if path.endswith("/comment"):
            return httpx.Response(200, json={"comments": [], "total": 0, "startAt": 0})
        return httpx.Response(
            200,
            json={
                "fields": {
                    "summary": "Checkout times out",
                    "description": adf(paragraph("It times out.")),
                    "issuetype": {"name": "Bug"},
                    "status": {"name": "In Progress"},
                    "priority": {"name": "High"},
                    "reporter": {"displayName": "Ada"},
                    "assignee": {"displayName": "Grace"},
                    "labels": ["checkout", "payments"],
                    "components": [{"name": "payments"}, {"name": "api"}],
                    "parent": {"key": "DEV-0"},
                    "subtasks": [{"key": "DEV-2"}, {"key": "DEV-3"}],
                    "issuelinks": [
                        {
                            "type": {"name": "blocks"},
                            "outwardIssue": {"key": "DEV-9", "fields": {"summary": "s"}},
                        },
                        {"type": {"name": "relates"}, "inwardIssue": {"key": "DEV-8"}},
                        "malformed",
                    ],
                    "fixVersions": [{"name": "2026.4"}],
                    "versions": [{"name": "2026.3"}],
                    "customfield_100": "Team Payments",
                    "customfield_101": {"value": "Critical"},
                    "customfield_102": ["a", {"name": "b"}],
                    "customfield_103": None,
                    "attachment": [],
                }
            },
        )

    provider = JiraCloudProvider(
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/", transport=httpx.MockTransport(handler)
        )
    )
    context = await provider.get_issue_context("DEV-1")
    assert context.issue_type == "Bug"
    assert context.status == "In Progress"
    assert context.priority == "High"
    assert context.reporter == "Ada"
    assert context.assignee == "Grace"
    assert context.labels == ["checkout", "payments"]
    assert context.components == ["payments", "api"]
    assert context.parent == "DEV-0"
    assert context.subtasks == ["DEV-2", "DEV-3"]
    assert {item.key for item in context.linked_issues} == {"DEV-9", "DEV-8"}
    assert context.fix_versions == ["2026.4"]
    assert context.affected_versions == ["2026.3"]
    assert context.custom_fields["customfield_100"] == "Team Payments"
    assert context.custom_fields["customfield_101"] == "Critical"
    assert context.custom_fields["customfield_102"] == "a, b"
    assert "customfield_103" not in context.custom_fields


@pytest.mark.asyncio
async def test_a_comment_page_that_stops_early_does_not_loop_forever():
    """A provider that under-reports its own total must still terminate."""

    def handler(request):
        if request.url.path.endswith("/comment"):
            return httpx.Response(
                200, json={"comments": [], "total": 500, "startAt": 0}
            )
        return httpx.Response(200, json={"fields": {"summary": "s", "description": None}})

    provider = JiraCloudProvider(
        httpx.AsyncClient(
            base_url="https://team.atlassian.net/", transport=httpx.MockTransport(handler)
        )
    )
    context = await provider.get_issue_context("DEV-1")
    assert context.comments == []
    assert context.truncations[0].total == 500
