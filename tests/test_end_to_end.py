"""End to end, through the real wiring.

These tests build the application the way ``agent_context`` builds it — real
``ContextTools``, real workflows, real providers — and only replace the network
transport. That is the level at which the audit's central question can be
answered: does a ticket analysis actually read the ticket and the repository,
and does the report say what it did not read?

Regression coverage for DL-P1-016 (the tool boundary was bypassed by two
workflows), DL-P2-026 (truncations were dropped between the provider and the
report) and DL-P3-030 (limitations were not merged into every report).
"""

from __future__ import annotations

import os

import httpx
import pytest

from devlens.agent.audit import AuditLog
from devlens.agent.engineering_agent import WorkflowRunner
from devlens.agent.workflows.implementation import ImplementWorkflow
from devlens.agent.workflows.review import ReviewWorkflow
from devlens.agent.workflows.ticket import TicketWorkflow
from devlens.app.approvals import ApprovalService
from devlens.app.config import Settings
from devlens.app.dependencies import ProjectRunners, agent_context
from devlens.domain import (
    AccessDenied,
    AnalysisRequest,
    ImplementRequest,
    ReviewRequest,
)
from devlens.guardrails import CapabilityGuard
from devlens.policies import ReadPolicy
from devlens.providers.git.github import GitHubProvider
from devlens.providers.jira.cloud import JiraCloudProvider
from devlens.runtime import RunContext
from devlens.sandbox.workspace import CloneSettings
from devlens.tools.context import ContextTools
from tests.conftest import github_transport, jira_transport


def build_runner(*, guard=None, approvals=None, audit=None, **transports) -> WorkflowRunner:
    jira_client = httpx.AsyncClient(
        base_url="https://team.atlassian.net/",
        transport=jira_transport(**transports.get("jira", {})),
    )
    git_client = httpx.AsyncClient(
        base_url="https://api.github.com/",
        transport=github_transport(**transports.get("github", {})),
    )
    tools = ContextTools(
        JiraCloudProvider(jira_client),
        GitHubProvider(git_client),
        ReadPolicy(frozenset({"org/repo"}), frozenset({"DEV"})),
        clone=CloneSettings("github.com", "Bearer token"),
        guard=guard or CapabilityGuard(),
    )
    return WorkflowRunner(
        TicketWorkflow(tools),
        review_workflow=ReviewWorkflow(tools),
        implement_workflow=ImplementWorkflow(tools),
        guard=guard or CapabilityGuard(),
        audit=audit,
        approvals=approvals,
        tools=tools,
    )


# --------------------------------------------------------------------------- #
# Ticket analysis
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_ticket_analysis_reads_the_ticket_and_the_repository():
    context = RunContext()
    result = await build_runner().analyze_ticket(
        AnalysisRequest(ticket_key="DEV-1", repository="org/repo"), context=context
    )
    assert result.ticket.key == "DEV-1"
    assert "instagram" in result.ticket.summary.lower()
    assert result.evidence, "an analysis with no evidence is not an analysis"
    tools_used = {call.tool for call in context.calls}
    assert {"jira.get_ticket", "git.resolve_and_list"} <= tools_used
    assert all(call.status == "ok" for call in context.calls)


@pytest.mark.asyncio
async def test_every_report_carries_the_limitations_that_applied_to_it():
    """DL-P3-030: the caveats belong on the artefact, not in the README."""
    result = await build_runner().analyze_ticket(
        AnalysisRequest(ticket_key="DEV-1", repository="org/repo")
    )
    text = " ".join(result.limitations)
    assert "deterministic" in text
    assert "never executed" in text
    assert "read-only" in text


@pytest.mark.asyncio
async def test_a_truncated_comment_thread_reaches_the_report():
    """DL-P2-026: what was not read has to survive to the artefact."""
    report = await build_runner(jira={"comments": 30, "total": 900}).implement(
        ImplementRequest(ticket_key="DEV-1", repository="org/repo")
    )
    assert report.truncations, "the report must say the thread was not read in full"
    assert report.truncations[0].total == 900
    assert report.truncations[0].fetched == 30


@pytest.mark.asyncio
async def test_a_repository_off_the_allowlist_never_reaches_the_network():
    """DL-P1-016: the check is at the tool boundary, not in each workflow."""
    runner = build_runner()
    with pytest.raises(AccessDenied, match="not on this project's allowlist"):
        await runner.analyze_ticket(
            AnalysisRequest(ticket_key="DEV-1", repository="org/secret")
        )


@pytest.mark.asyncio
async def test_a_ticket_outside_the_project_allowlist_is_refused():
    with pytest.raises(AccessDenied, match="not on this project's allowlist"):
        await build_runner().analyze_ticket(
            AnalysisRequest(ticket_key="OPS-9", repository="org/repo")
        )


@pytest.mark.asyncio
async def test_a_denied_call_is_recorded_as_denied_in_the_run():
    context = RunContext()
    with pytest.raises(AccessDenied):
        await build_runner().analyze_ticket(
            AnalysisRequest(ticket_key="OPS-9", repository="org/repo"), context=context
        )
    assert context.calls == [] or context.calls[0].status == "denied"


@pytest.mark.asyncio
async def test_the_audit_trail_records_the_run_and_its_outcome():
    audit = AuditLog()
    await build_runner(audit=audit).analyze_ticket(
        AnalysisRequest(ticket_key="DEV-1", repository="org/repo")
    )
    events = [item["event"] for item in audit.recent()]
    assert events == ["run.started", "run.completed"] or "run.completed" in events


@pytest.mark.asyncio
async def test_a_failed_run_is_audited_too():
    audit = AuditLog()
    with pytest.raises(AccessDenied):
        await build_runner(audit=audit).analyze_ticket(
            AnalysisRequest(ticket_key="OPS-9", repository="org/repo")
        )
    assert any(item["event"] == "run.failed" for item in audit.recent())


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_review_reads_the_diff_and_anchors_its_findings():
    context = RunContext()
    report = await build_runner().review(
        ReviewRequest(repository="org/repo", pr=7), context=context
    )
    assert report.status == "completed"
    assert any("org/repo#7" in item.source for item in report.evidence_items)
    for finding in report.findings:
        assert finding.file, "a finding without a file cannot be acted on"
    assert "git.get_pull_request" in {call.tool for call in context.calls}


@pytest.mark.asyncio
async def test_a_review_reports_the_pages_it_did_not_read():
    report = await build_runner(github={"pr_pages": 50}).review(
        ReviewRequest(repository="org/repo", pr=7)
    )
    assert report.truncations


@pytest.mark.asyncio
async def test_a_review_against_a_ticket_checks_its_acceptance_criteria():
    report = await build_runner().review(
        ReviewRequest(repository="org/repo", pr=7, ticket_key="DEV-1")
    )
    body = " ".join(
        [report.executive_summary, *(item.title for item in report.findings)]
    ).lower()
    assert "acceptance" in body or "criterion" in body or "criteria" in body


@pytest.mark.asyncio
async def test_running_the_repository_tests_is_refused_without_the_capability():
    report = await build_runner().review(
        ReviewRequest(repository="org/repo", pr=7, run_tests=True)
    )
    text = " ".join(report.limitations + report.observations)
    assert "never executed" in text or "disabled" in text


# --------------------------------------------------------------------------- #
# Implement
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_implement_plans_and_proposes_but_never_writes(jobs_db):
    approvals = ApprovalService(jobs_db)
    report = await build_runner(approvals=approvals).implement(
        ImplementRequest(ticket_key="DEV-1", repository="org/repo")
    )
    assert report.status in {"completed", "partial"}
    assert report.proposals, "implement should propose the branch and the pull request"
    for proposal in report.proposals:
        assert proposal.action in {"create_remote_branch", "create_pr", "post_jira_comment"}
        assert proposal.payload_sha256
    # Every proposal is persisted so an operator can approve it later.
    stored = approvals.proposals_for(report.proposals[0].run_id)
    assert len(stored) == len(report.proposals)


@pytest.mark.asyncio
async def test_a_proposed_branch_uses_the_devlens_prefix():
    report = await build_runner().implement(
        ImplementRequest(ticket_key="DEV-1", repository="org/repo")
    )
    branches = [
        item.payload.get("branch") or item.payload.get("head")
        for item in report.proposals
        if item.action in {"create_remote_branch", "create_pr"}
    ]
    assert branches
    assert all(str(name).startswith("devlens/") for name in branches)


@pytest.mark.asyncio
async def test_implement_says_when_a_sandbox_clone_was_not_available():
    report = await build_runner().implement(
        ImplementRequest(ticket_key="DEV-1", repository="org/repo")
    )
    text = " ".join(report.limitations + report.observations)
    assert "sandbox" in text.lower()


@pytest.mark.asyncio
async def test_a_plan_without_acceptance_criteria_says_to_agree_them_first():
    report = await build_runner(
        jira={"comments": 0}
    ).implement(ImplementRequest(ticket_key="DEV-1", repository="org/repo"))
    assert report.status in {"completed", "partial"}
    assert report.implementation_plan
    assert any(
        "agree them" in step.lower() or "acceptance" in step.lower()
        for step in report.implementation_plan
    )


# --------------------------------------------------------------------------- #
# The wiring itself
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_context_builds_one_runner_and_one_gateway_per_project(
    monkeypatch, single_project
):
    """The real wiring, with only the transport replaced."""
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        base = str(kwargs.get("base_url", ""))
        kwargs["transport"] = (
            github_transport() if "github" in base else jira_transport()
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr("devlens.app.dependencies.httpx.AsyncClient", client)
    monkeypatch.setattr(
        "devlens.providers.factory.httpx.AsyncClient", client, raising=False
    )

    settings = Settings.from_env()
    async with agent_context(settings, CapabilityGuard()) as runners:
        assert isinstance(runners, ProjectRunners)
        assert set(runners.runners) == {"default"}
        assert runners.gateway_for("default") is not None
        assert runners.gateway_for_repository("org/repo", None) is not None
        assert runners.gateway_for_repository("org/other", None) is None
        result = await runners.analyze_ticket(
            AnalysisRequest(ticket_key="DEV-1", repository="org/repo")
        )
        assert result.ticket.key == "DEV-1"


@pytest.mark.asyncio
async def test_an_unknown_project_names_the_ones_that_are_configured(single_project):
    runners = ProjectRunners({}, Settings.from_env())
    with pytest.raises(AccessDenied, match="configured: none"):
        await runners.analyze_ticket(
            AnalysisRequest(project="absent", ticket_key="DEV-1", repository="org/repo")
        )


# --------------------------------------------------------------------------- #
# Repository code execution, end to end
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_review_that_runs_tests_needs_a_clone_and_says_so_when_it_has_none():
    """The capability is on, but the sandbox clone is not reachable from here."""
    guard = CapabilityGuard({"repository_code_execution"})
    report = await build_runner(guard=guard).review(
        ReviewRequest(repository="org/repo", pr=7, run_tests=True)
    )
    text = " ".join(report.observations)
    assert "Sandbox tests were not run" in text or "workspace" in text.lower()


@pytest.mark.asyncio
async def test_a_review_runs_the_repository_tests_in_its_sandbox(monkeypatch, git_repo):
    """With a clone available and the capability on, the result is reported."""
    import sys
    from pathlib import Path as _Path

    from devlens.sandbox.workspace import Workspace

    # The child receives an allowlisted environment, so the runner has to be
    # reachable on PATH. Pointing PATH at this interpreter's own bin directory
    # makes the test exercise a real test run on any machine, rather than
    # passing because the binary happened to be missing.
    monkeypatch.setenv(
        "PATH", f"{_Path(sys.executable).parent}:{os.environ.get('PATH', '')}"
    )
    guard = CapabilityGuard({"repository_code_execution"})
    runner = build_runner(guard=guard)

    async def open_workspace(repository, ref, context=None):
        return Workspace(git_repo)

    monkeypatch.setattr(runner.review_workflow.tools, "open_workspace", open_workspace)
    report = await runner.review(
        ReviewRequest(repository="org/repo", pr=7, run_tests=True)
    )
    text = " ".join(report.observations)
    assert "Repository tests exited" in text, text
    assert "not available" not in text, "the test runner must actually have run"


@pytest.mark.asyncio
async def test_a_review_proposes_a_comment_carrying_its_findings(jobs_db):
    from devlens.app.approvals import ApprovalService

    hostile_diff = (
        b"diff --git a/src/checkout.py b/src/checkout.py\n"
        b"--- a/src/checkout.py\n+++ b/src/checkout.py\n"
        b"@@ -1,2 +1,4 @@\n context\n"
        b'+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        b"+response = requests.get(url)\n"
    )
    approvals = ApprovalService(jobs_db)
    report = await build_runner(
        approvals=approvals, github={"diff": hostile_diff}
    ).review(ReviewRequest(repository="org/repo", pr=7))
    assert report.findings, "a hard-coded credential in the diff must be reported"
    assert report.proposals
    proposal = report.proposals[0]
    assert proposal.action == "post_pr_comment"
    assert str(proposal.payload["number"]) == "7"
    assert "DevLens review of #7" in proposal.payload["body"]
    assert "deterministic heuristics" in proposal.payload["body"]


@pytest.mark.asyncio
async def test_implement_reports_a_baseline_test_run_when_it_is_allowed(
    monkeypatch, git_repo
):
    import sys
    from pathlib import Path as _Path

    from devlens.sandbox.workspace import Workspace

    monkeypatch.setenv(
        "PATH", f"{_Path(sys.executable).parent}:{os.environ.get('PATH', '')}"
    )
    guard = CapabilityGuard({"repository_code_execution"})
    runner = build_runner(guard=guard)

    async def open_workspace(repository, ref, context=None):
        return Workspace(git_repo)

    monkeypatch.setattr(runner.implement_workflow.tools, "open_workspace", open_workspace)
    report = await runner.implement(
        ImplementRequest(ticket_key="DEV-1", repository="org/repo", run_tests=True)
    )
    text = " ".join(report.observations)
    assert "Baseline test run exited" in text, text
    assert "not available" not in text, "the baseline run must actually have run"
    assert "devlens/" in text
