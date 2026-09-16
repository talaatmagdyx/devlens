"""Proposals, approvals and the write boundary.

Regression coverage for DL-P1-004 (an approval could execute twice),
DL-P1-005 (an approval never expired) and DL-P1-006 (the payload was not
re-checked at execution time, so an approved comment could become a different
one). The human-only refusal is checked from two directions, because a single
`if` in one code path was what the audit found.
"""

from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from devlens.agent.audit import AuditLog
from devlens.app.approvals import ApprovalService
from devlens.domain import (
    AccessDenied,
    Conflict,
    Proposal,
    ProposalCreate,
    ProviderError,
)
from devlens.guardrails import CapabilityGuard
from devlens.policies import ReadPolicy
from devlens.providers.writes import WriteGateway, branch_for

# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class RecordingGit:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def create_branch(self, repository, branch, sha):
        self.calls.append(("create_branch", repository, branch, sha))

    async def resolve_ref(self, repository, ref):
        return "c" * 40

    async def default_branch(self, repository):
        return "main"

    async def create_pull_request(self, repository, title, head, base, body):
        self.calls.append(("create_pr", repository, title, head, base, body))
        return f"https://github.com/{repository}/pull/1"

    async def post_pr_comment(self, repository, number, body):
        self.calls.append(("post_pr_comment", repository, number, body))


class RecordingJira:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def post_comment(self, ticket_key, body):
        self.calls.append((ticket_key, body))


class SlowGateway:
    """A gateway that yields control, so a second approval can interleave."""

    allowed = True

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, proposal, approved_hash) -> str:
        self.executions += 1
        await asyncio.sleep(0.05)
        return "done"


def policy() -> ReadPolicy:
    return ReadPolicy(frozenset({"org/repo"}), frozenset({"DEV"}))


def gateway(**overrides) -> WriteGateway:
    guard = overrides.pop("guard", CapabilityGuard({"git_writeback", "jira_writeback"}))
    return WriteGateway(
        git=overrides.pop("git", RecordingGit()),
        jira=overrides.pop("jira", RecordingJira()),
        guard=guard,
        policy=overrides.pop("policy", policy()),
    )


def proposal(**overrides) -> Proposal:
    fields = {
        "id": str(uuid4()),
        "run_id": "run-1",
        "action": "post_pr_comment",
        "repository": "org/repo",
        "target": "org/repo#7",
        "payload": {"number": 7, "body": "Three findings, none blocking."},
        "rationale": "Publish the review.",
        "created_at": time.time(),
    }
    fields.update(overrides)
    return Proposal(**fields)


@pytest.fixture
def service(jobs_db) -> ApprovalService:
    return ApprovalService(jobs_db, audit=AuditLog())


# --------------------------------------------------------------------------- #
# Single execution
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_concurrent_approvals_execute_exactly_once(service):
    """DL-P1-004. One caller wins; the other is told, before any network call."""
    record = service.request(service.propose(proposal()).id)
    remote = SlowGateway()

    outcomes = await asyncio.gather(
        service.approve(record.id, remote),
        service.approve(record.id, remote),
        return_exceptions=True,
    )

    assert remote.executions == 1
    succeeded = [item for item in outcomes if not isinstance(item, Exception)]
    refused = [item for item in outcomes if isinstance(item, Conflict)]
    assert len(succeeded) == 1 and len(refused) == 1
    assert "already" in str(refused[0])
    assert service.get(record.id).status == "approved"


@pytest.mark.asyncio
async def test_approving_twice_in_sequence_is_refused(service):
    record = service.request(service.propose(proposal()).id)
    remote = SlowGateway()
    await service.approve(record.id, remote)
    with pytest.raises(Conflict, match="already approved"):
        await service.approve(record.id, remote)
    assert remote.executions == 1


@pytest.mark.asyncio
async def test_a_proposal_is_executed_once_even_across_two_approvals(service):
    """The gateway's own idempotency ledger is the second line of defence."""
    stored = service.propose(proposal())
    remote = gateway()
    first = service.request(stored.id)
    await service.approve(first.id, remote)
    second = service.request(stored.id)
    await service.approve(second.id, remote)
    assert len(remote.git.calls) == 1


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_expired_approval_cannot_be_executed(jobs_db):
    """DL-P1-005. Yesterday's approval is not authority for today's write."""
    service = ApprovalService(jobs_db, ttl=-1.0)
    record = service.request(service.propose(proposal()).id)
    remote = SlowGateway()
    with pytest.raises(Conflict, match="expired"):
        await service.approve(record.id, remote)
    assert remote.executions == 0
    assert service.get(record.id).status == "expired"


def test_expiry_is_swept_when_the_queue_is_listed(jobs_db):
    service = ApprovalService(jobs_db, ttl=-1.0)
    service.request(service.propose(proposal()).id)
    assert [item.status for item in service.list()] == ["expired"]


def test_a_live_approval_is_not_swept(service):
    service.request(service.propose(proposal()).id)
    assert service.expire_due() == 0
    assert [item.status for item in service.list()] == ["pending"]


# --------------------------------------------------------------------------- #
# The seal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_payload_edited_after_approval_is_refused(service):
    """DL-P1-006, exercised through the database rather than the object graph."""
    stored = service.propose(proposal())
    record = service.request(stored.id)
    with service.db.write() as conn:
        conn.execute(
            "UPDATE proposals SET payload_json = ? WHERE id = ?",
            (json.dumps({"number": 7, "body": "Ship it, no issues found."}), stored.id),
        )

    remote = gateway()
    outcome = await service.approve(record.id, remote)
    assert outcome.status == "rejected"
    assert "changed after it was approved" in outcome.result
    assert remote.git.calls == []


def test_a_proposal_cannot_be_constructed_with_a_foreign_hash():
    with pytest.raises(ValidationError, match="does not match its recorded hash"):
        proposal(payload_sha256="0" * 64)


def test_the_hash_covers_the_target_and_the_action_not_just_the_body():
    base = proposal()
    assert proposal(payload=base.payload, target="org/repo#8").payload_sha256 != (
        base.payload_sha256
    )
    assert proposal(
        payload=base.payload, action="post_jira_comment", ticket_key="DEV-1"
    ).payload_sha256 != base.payload_sha256


def test_verify_seal_accepts_the_untouched_payload():
    stored = proposal()
    gateway().verify_seal(stored, stored.payload_sha256)


# --------------------------------------------------------------------------- #
# What cannot be written at all
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ["merge_pr", "deploy", "release"])
def test_human_only_actions_cannot_even_be_represented(action):
    with pytest.raises(ValidationError):
        proposal(action=action)
    with pytest.raises(ValidationError):
        ProposalCreate(
            action=action, run_id="r", target="org/repo", rationale="x"
        )


@pytest.mark.parametrize("action", ["merge_pr", "deploy", "release"])
def test_the_gateway_refuses_them_again_at_the_boundary(action):
    """Belt and braces: even a proposal that bypassed validation is refused."""
    smuggled = proposal().model_copy()
    object.__setattr__(smuggled, "action", action)
    with pytest.raises(AccessDenied, match="human-only"):
        gateway().authorize(smuggled)


@pytest.mark.asyncio
async def test_a_repository_off_the_allowlist_is_refused_at_the_write_boundary():
    """The read path allowlist is not trusted; the write path re-checks."""
    with pytest.raises(AccessDenied, match="not on this project's allowlist"):
        await gateway().execute(
            (stored := proposal(repository="org/other", target="org/other#1")),
            stored.payload_sha256,
        )


@pytest.mark.asyncio
async def test_a_jira_comment_outside_the_project_allowlist_is_refused():
    stored = proposal(
        action="post_jira_comment",
        repository=None,
        ticket_key="OPS-4",
        target="OPS-4",
        payload={"body": "analysis"},
    )
    with pytest.raises(AccessDenied, match="not on this project's allowlist"):
        await gateway().execute(stored, stored.payload_sha256)


@pytest.mark.asyncio
async def test_writes_are_refused_when_the_capability_is_off():
    remote = gateway(guard=CapabilityGuard())
    assert remote.allowed is False
    stored = proposal()
    with pytest.raises(AccessDenied):
        await remote.execute(stored, stored.payload_sha256)


@pytest.mark.asyncio
async def test_approving_without_write_back_records_the_decision_only(service):
    """The operator is told plainly that nothing left the machine."""
    record = service.request(service.propose(proposal()).id)
    outcome = await service.approve(record.id, gateway(guard=CapabilityGuard()))
    assert outcome.status == "approved"
    assert "nothing was sent to the remote" in outcome.result
    assert "DEVLENS_ALLOW_WRITES" in outcome.result


@pytest.mark.asyncio
async def test_a_placeholder_body_is_refused():
    for body in ("", "   ", None):
        stored = proposal(payload={"number": 7, "body": body})
        with pytest.raises(ProviderError, match="does not post placeholder"):
            await gateway().execute(stored, stored.payload_sha256)


@pytest.mark.asyncio
async def test_only_devlens_prefixed_branches_may_be_created():
    stored = proposal(
        action="create_remote_branch", target="org/repo", payload={"branch": "main"}
    )
    with pytest.raises(AccessDenied, match="devlens/"):
        await gateway().execute(stored, stored.payload_sha256)
    assert branch_for("DEV-1", "org/repo").startswith("devlens/dev-1-")


@pytest.mark.asyncio
async def test_a_branch_name_that_is_not_a_valid_ref_is_refused():
    stored = proposal(
        action="create_remote_branch",
        target="org/repo",
        payload={"branch": "devlens/a b:c"},
    )
    with pytest.raises(AccessDenied, match="not a valid git ref"):
        await gateway().execute(stored, stored.payload_sha256)


@pytest.mark.asyncio
async def test_an_authorised_write_reaches_the_remote_with_the_approved_payload():
    stored = proposal()
    remote = gateway()
    result = await remote.execute(stored, stored.payload_sha256)
    assert remote.git.calls == [
        ("post_pr_comment", "org/repo", 7, "Three findings, none blocking.")
    ]
    assert "org/repo#7" in result


# --------------------------------------------------------------------------- #
# Queue behaviour
# --------------------------------------------------------------------------- #


def test_rejecting_a_decided_approval_is_a_conflict(service):
    record = service.request(service.propose(proposal()).id)
    assert service.reject(record.id).status == "rejected"
    with pytest.raises(Conflict, match="already rejected"):
        service.reject(record.id)


def test_requesting_an_approval_for_an_unknown_proposal_fails(service):
    with pytest.raises(ProviderError, match="No such proposal"):
        service.request("does-not-exist")


def test_the_queue_is_bounded(service, monkeypatch):
    monkeypatch.setattr("devlens.app.approvals.MAX_PENDING", 3)
    stored = service.propose(proposal())
    for _ in range(3):
        service.request(stored.id)
    with pytest.raises(ProviderError, match="queue is full"):
        service.request(stored.id)


def test_approvals_survive_a_restart(jobs_db):
    first = ApprovalService(jobs_db)
    record = first.request(first.propose(proposal()).id)
    first.db.close()

    second = ApprovalService(jobs_db)
    reopened = second.get(record.id)
    assert reopened is not None
    assert reopened.status == "pending"
    assert second.proposal(reopened.proposal_id).payload["number"] == 7


def test_the_audit_trail_records_each_decision_without_the_payload(jobs_db):
    audit = AuditLog()
    service = ApprovalService(jobs_db, audit=audit)
    record = service.request(service.propose(proposal()).id)
    service.reject(record.id)
    events = [entry["event"] for entry in audit.recent()]
    assert events == ["proposal.created", "approval.requested", "approval.rejected"]


def test_audit_redaction_is_recursive_and_covers_urls():
    audit = AuditLog()
    entry = audit.record(
        {
            "event": "x",
            "ticket_key": "DEV-1",
            "nested": {"list": [{"github_token": "ghp_" + "a" * 36}]},
            "url": "https://user:pw@api.example.com/x?access_token=abc&page=2",
            "text": "leaked sk-" + "b" * 20,
        }
    )
    assert entry["ticket_key"] == "DEV-1"
    assert entry["nested"]["list"][0]["github_token"] == "***"
    assert "pw@" not in entry["url"] and "abc" not in entry["url"]
    assert "page=2" in entry["url"]
    assert "sk-" not in entry["text"]


# --------------------------------------------------------------------------- #
# Each write action, executed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_branch_is_created_at_the_analysed_commit():
    stored = proposal(
        action="create_remote_branch",
        target="org/repo@devlens/dev-1",
        payload={"branch": "devlens/dev-1", "sha": "d" * 40},
    )
    remote = gateway()
    result = await remote.execute(stored, stored.payload_sha256)
    assert remote.git.calls == [("create_branch", "org/repo", "devlens/dev-1", "d" * 40)]
    assert "devlens/dev-1" in result


@pytest.mark.asyncio
async def test_a_branch_without_a_sha_resolves_head_first():
    stored = proposal(
        action="create_remote_branch",
        target="org/repo@devlens/dev-1",
        payload={"branch": "devlens/dev-1"},
    )
    remote = gateway()
    await remote.execute(stored, stored.payload_sha256)
    assert remote.git.calls[0][3] == "c" * 40


@pytest.mark.asyncio
async def test_a_pull_request_is_opened_against_the_default_branch():
    stored = proposal(
        action="create_pr",
        target="org/repo",
        payload={"head": "devlens/dev-1", "body": "the analysis"},
    )
    remote = gateway()
    result = await remote.execute(stored, stored.payload_sha256)
    action, repository, title, head, base, body = remote.git.calls[0]
    assert (action, repository, head, base) == ("create_pr", "org/repo", "devlens/dev-1", "main")
    assert title.startswith("DevLens: ")
    assert body == "the analysis"
    assert result.startswith("https://github.com/")


@pytest.mark.asyncio
async def test_a_pull_request_title_is_bounded():
    stored = proposal(
        action="create_pr",
        target="org/repo",
        payload={"head": "devlens/dev-1", "title": "t" * 400, "body": "b"},
    )
    remote = gateway()
    await remote.execute(stored, stored.payload_sha256)
    assert len(remote.git.calls[0][2]) == 250


@pytest.mark.asyncio
async def test_a_jira_comment_reaches_the_ticket():
    stored = proposal(
        action="post_jira_comment",
        repository=None,
        ticket_key="DEV-1",
        target="DEV-1",
        payload={"body": "three findings"},
    )
    remote = gateway()
    result = await remote.execute(stored, stored.payload_sha256)
    assert remote.jira.calls == [("DEV-1", "three findings")]
    assert "DEV-1" in result


@pytest.mark.asyncio
async def test_a_pull_request_comment_needs_a_number():
    stored = proposal(payload={"number": 0, "body": "findings"})
    with pytest.raises(ProviderError, match="pull request number is required"):
        await gateway().execute(stored, stored.payload_sha256)


@pytest.mark.asyncio
async def test_a_write_body_is_capped():
    from devlens.providers.writes import MAX_BODY

    stored = proposal(payload={"number": 7, "body": "x" * (MAX_BODY + 5_000)})
    remote = gateway()
    await remote.execute(stored, stored.payload_sha256)
    assert len(remote.git.calls[0][3]) == MAX_BODY


@pytest.mark.asyncio
async def test_a_write_without_an_attached_client_is_a_provider_error():
    stored = proposal()
    with pytest.raises(ProviderError, match="Git write-back is not attached"):
        await gateway(git=None).execute(stored, stored.payload_sha256)

    jira_proposal = proposal(
        action="post_jira_comment",
        repository=None,
        ticket_key="DEV-1",
        target="DEV-1",
        payload={"body": "x"},
    )
    with pytest.raises(ProviderError, match="Jira write-back is not attached"):
        await gateway(jira=None).execute(jira_proposal, jira_proposal.payload_sha256)


@pytest.mark.asyncio
async def test_a_jira_comment_without_a_ticket_key_is_refused():
    stored = proposal(
        action="post_jira_comment", repository=None, target="x", payload={"body": "b"}
    )
    with pytest.raises(AccessDenied, match="requires a ticket key"):
        await gateway().execute(stored, stored.payload_sha256)


@pytest.mark.asyncio
async def test_a_git_write_without_a_repository_is_refused():
    stored = proposal(repository=None, target="x", payload={"number": 7, "body": "b"})
    with pytest.raises(AccessDenied, match="requires a repository"):
        await gateway().execute(stored, stored.payload_sha256)


def test_an_unattached_gateway_reports_that_it_cannot_write():
    from devlens.providers.writes import WriteGateway

    assert WriteGateway().allowed is False
