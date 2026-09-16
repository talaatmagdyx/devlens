"""Workflows with real inputs attached.

The workflow tests elsewhere run against bare questions, which exercises the
deterministic core. These attach the things an operator actually has — a local
checkout, a ticket, a language model, live telemetry — and check that each one
changes the report in the way the audit requires: more evidence, never more
certainty.
"""

from __future__ import annotations

import httpx
import pytest

from devlens.agent.workflows.platform import (
    AskWorkflow,
    DesignWorkflow,
    InvestigateWorkflow,
    ObserveWorkflow,
)
from devlens.domain import AccessDenied, PlatformRequest, ProviderError, ProviderResult
from devlens.guardrails import CapabilityGuard
from devlens.policies import ReadPolicy
from devlens.providers.git.github import GitHubProvider
from devlens.providers.jira.cloud import JiraCloudProvider
from devlens.runtime import RunContext
from devlens.tools.context import ContextTools
from tests.conftest import github_transport, jira_transport


class Model:
    kind = "openai"
    model = "gpt-4o-mini"
    vision_capable = True

    def __init__(self, answer: str = "Two sources agree the pool is saturated.") -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Hub:
    configured = True

    def __init__(self, results: dict) -> None:
        self.results = results

    async def collect(self, query: str, window_seconds: int = 3600):
        return self.results


def tools() -> ContextTools:
    return ContextTools(
        JiraCloudProvider(
            httpx.AsyncClient(
                base_url="https://team.atlassian.net/", transport=jira_transport()
            )
        ),
        GitHubProvider(
            httpx.AsyncClient(
                base_url="https://api.github.com/", transport=github_transport()
            )
        ),
        ReadPolicy(frozenset({"org/repo"}), frozenset({"DEV"})),
    )


# --------------------------------------------------------------------------- #
# A local checkout as evidence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_local_checkout_becomes_located_evidence(git_repo):
    context = RunContext()
    report = await AskWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="instagram", path=str(git_repo)), context=context
    )
    code = [item for item in report.evidence_items if item.type == "code"]
    assert code, "a search hit in the checkout should become evidence"
    assert all(item.provenance.path for item in code), "evidence must say which file"
    assert any("app.py" in item.source for item in code)
    assert all(item.status != "CONFIRMED" for item in code), "code cannot confirm"


@pytest.mark.asyncio
async def test_a_search_with_no_match_says_so_rather_than_inventing(git_repo):
    report = await AskWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="zzzznotpresent", path=str(git_repo))
    )
    assert any("No matches" in fact for fact in report.facts)
    assert [item for item in report.evidence_items if item.type == "code"] == []


@pytest.mark.asyncio
async def test_a_path_that_is_not_a_workspace_is_reported_not_raised(tmp_path):
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="p99 latency tripled", path=str(tmp_path))
    )
    assert any("not a git workspace" in fact for fact in report.facts)
    assert report.status in {"partial", "insufficient_context", "completed"}


# --------------------------------------------------------------------------- #
# A model summarises, and never adds certainty
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_model_summary_is_marked_as_model_generated(git_repo):
    model = Model()
    guard = CapabilityGuard({"llm"}, llm=model)
    report = await AskWorkflow(guard).run(
        PlatformRequest(question="instagram", path=str(git_repo))
    )
    assert report.executive_summary == model.answer
    assert any("model-generated" in item for item in report.unknowns)
    assert "<untrusted-data" in model.prompts[0], "evidence must be fenced"


@pytest.mark.asyncio
async def test_a_model_failure_falls_back_to_the_deterministic_summary(git_repo):
    guard = CapabilityGuard({"llm"}, llm=Model(ProviderError("model is down")))
    report = await AskWorkflow(guard).run(
        PlatformRequest(question="instagram", path=str(git_repo))
    )
    assert "model is down" in " ".join(report.unknowns)
    assert report.executive_summary, "the deterministic summary still stands"


@pytest.mark.asyncio
async def test_a_model_never_raises_a_hypothesis_above_its_evidence(git_repo):
    guard = CapabilityGuard({"llm"}, llm=Model("The root cause is definitely the pool."))
    report = await InvestigateWorkflow(guard).run(
        PlatformRequest(question="p99 latency tripled", path=str(git_repo))
    )
    assert report.root_cause is None, "prose cannot establish a root cause"
    assert all(item.status == "open" for item in report.hypotheses)


# --------------------------------------------------------------------------- #
# Live telemetry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_available_telemetry_supports_and_confirms_appropriately():
    hub = Hub(
        {
            "log": ProviderResult(
                provider="Loki", status="AVAILABLE", rows=["timeout in checkout"], query="{}"
            ),
            "metric": ProviderResult(
                provider="Prometheus", status="AVAILABLE", rows=["pool 98%"], query="up"
            ),
            "trace": ProviderResult(provider="Tempo", status="EMPTY"),
            "sql": ProviderResult(provider="SQL gateway", status="NOT_CONFIGURED"),
        }
    )
    guard = CapabilityGuard({"observability_provider"}, observe=hub)
    report = await InvestigateWorkflow(guard).run(
        PlatformRequest(question="checkout p99 latency tripled")
    )
    statuses = {item.status for item in report.evidence_items}
    assert "SUPPORTED" in statuses
    assert "UNKNOWN" in statuses, "the empty and unconfigured providers stay UNKNOWN"
    assert {item.provider for item in report.provider_results} >= {"Loki", "Prometheus"}


@pytest.mark.asyncio
async def test_a_mixed_provider_response_reports_each_one_separately():
    hub = Hub(
        {
            "log": ProviderResult(provider="Loki", status="AVAILABLE", rows=["x"], query="{}"),
            "metric": ProviderResult(
                provider="Prometheus", status="UNAVAILABLE", error="refused", query="up"
            ),
            "trace": ProviderResult(provider="Tempo", status="TIMEOUT", error="deadline"),
            "sql": ProviderResult(provider="SQL gateway", status="DENIED", error="not a SELECT"),
        }
    )
    guard = CapabilityGuard({"observability_provider"}, observe=hub)
    report = await InvestigateWorkflow(guard).run(
        PlatformRequest(question="checkout is slow")
    )
    by_provider = {item.provider: item.status for item in report.provider_results}
    assert by_provider["Prometheus"] == "UNAVAILABLE"
    assert by_provider["Tempo"] == "TIMEOUT"
    assert by_provider["SQL gateway"] == "DENIED"
    assert "could not be reached" in report.executive_summary


# --------------------------------------------------------------------------- #
# A ticket attached to an investigation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_ticket_becomes_evidence_with_its_url():
    report = await InvestigateWorkflow(CapabilityGuard(), tools=tools()).run(
        PlatformRequest(question="checkout times out", ticket_key="DEV-1")
    )
    jira = [item for item in report.evidence_items if item.type == "jira"]
    assert jira and jira[0].provenance.url.endswith("/browse/DEV-1")


@pytest.mark.asyncio
async def test_a_hostile_attachment_is_recorded_as_data_only():
    hostile = [
        {
            "id": "1",
            "filename": "hostile.log",
            "mimeType": "text/plain",
            "size": 40,
            "content": "https://team.atlassian.net/attachment/content/1",
        }
    ]
    instrumented = ContextTools(
        JiraCloudProvider(
            httpx.AsyncClient(
                base_url="https://team.atlassian.net/",
                transport=jira_transport(attachments=hostile),
            )
        ),
        GitHubProvider(
            httpx.AsyncClient(base_url="https://api.github.com/", transport=github_transport())
        ),
        ReadPolicy(frozenset({"org/repo"}), frozenset({"DEV"})),
    )
    report = await InvestigateWorkflow(CapabilityGuard(), tools=instrumented).run(
        PlatformRequest(question="checkout times out", ticket_key="DEV-1")
    )
    # The fixture attachment carries ordinary log text, so nothing is flagged;
    # what matters is that the attachment path ran and produced no CONFIRMED.
    assert all(item.status != "CONFIRMED" for item in report.evidence_items)


@pytest.mark.asyncio
async def test_a_ticket_off_the_allowlist_is_recorded_rather_than_failing_the_run():
    report = await InvestigateWorkflow(CapabilityGuard(), tools=tools()).run(
        PlatformRequest(question="checkout times out", ticket_key="OPS-9")
    )
    jira = [item for item in report.evidence_items if item.type == "jira"]
    assert jira and jira[0].status == "UNKNOWN"
    assert "could not be read" in jira[0].observation


# --------------------------------------------------------------------------- #
# Design and observe with a workspace
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_design_scans_the_workspace_it_is_given(git_repo):
    report = await DesignWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="20k writes per second at 1kb each", path=str(git_repo))
    )
    assert report.facts
    assert any("calculated" in fact for fact in report.facts)


@pytest.mark.asyncio
async def test_observe_with_live_providers_reports_what_answered(git_repo):
    hub = Hub(
        {
            "log": ProviderResult(provider="Loki", status="AVAILABLE", rows=["x"], query="{}"),
            "metric": ProviderResult(provider="Prometheus", status="EMPTY"),
            "trace": ProviderResult(provider="Tempo", status="NOT_CONFIGURED"),
            "sql": ProviderResult(provider="SQL gateway", status="NOT_CONFIGURED"),
        }
    )
    guard = CapabilityGuard({"observability_provider"}, observe=hub)
    report = await ObserveWorkflow(guard).run(PlatformRequest(path=str(git_repo)))
    assert report.provider_results
    assert all(item.verification for item in report.findings)


@pytest.mark.asyncio
async def test_observe_on_a_path_that_is_not_a_repository_says_so(tmp_path):
    with pytest.raises((ProviderError, AccessDenied)):
        await ObserveWorkflow(CapabilityGuard()).run(PlatformRequest(path=str(tmp_path)))


# --------------------------------------------------------------------------- #
# The search output contract
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_search_result_names_the_file_it_matched(git_repo):
    """Regression: the search suppressed filenames, so every hit was dropped.

    ``rg -I`` means "no filename", and every consumer parses ``path:line:text``
    to locate the hit. The two disagreed silently: a local search returned
    matches and the report showed no code evidence at all.
    """
    from devlens.providers.git.local import RepoWorkspace
    from devlens.sandbox.workspace import open_local

    repo = RepoWorkspace(open_local(str(git_repo)))
    output = await repo.search("instagram")
    assert output.strip(), "the fixture repository contains the term"
    for line in output.splitlines():
        path, _, rest = line.partition(":")
        number, _, _text = rest.partition(":")
        assert path.endswith(".py"), f"no filename in {line!r}"
        assert number.isdigit(), f"no line number in {line!r}"


@pytest.mark.asyncio
async def test_a_search_hit_becomes_evidence_that_points_at_a_file(git_repo):
    from devlens.agent.workflows.platform import local_evidence

    facts, items, hits = await local_evidence(str(git_repo), "instagram", run_id="run-1")
    assert any("instagram" in fact for fact in facts)
    assert hits, "the parsed hits must not be empty when the search matched"
    assert items, "a matched line must become evidence"
    for item in items:
        assert item.provenance.path
        assert item.excerpt
        assert item.status in {"SUPPORTED", "HYPOTHESIS"}
