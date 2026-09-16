"""DevLens domain model.

Three orthogonal scales that must never be conflated:

===============  ===========================================  =========================================
Scale            Question it answers                          Values
===============  ===========================================  =========================================
Severity         How bad is this finding if it is real?       P0 P1 P2 P3
EvidenceStatus   What does the evidence establish?            CONFIRMED SUPPORTED HYPOTHESIS UNKNOWN
                                                              REJECTED
ConfidenceBand   How sure is DevLens that it is right?        LOW MEDIUM HIGH VERY_HIGH
===============  ===========================================  =========================================

Every piece of evidence carries :class:`Provenance`. An :class:`EvidenceItem`
may only claim ``CONFIRMED`` or ``SUPPORTED`` when its provenance records a
successful retrieval, and the model validator refuses anything else. A provider
outage therefore cannot be filed as support for a hypothesis: the invariant is
enforced by the type system rather than by the discipline of each workflow.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEVLENS_VERSION = "0.2.0"
WORKFLOW_VERSION = "2"

TICKET_PATTERN = r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$"
REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
PROJECT_PATTERN = r"^[A-Za-z0-9_-]+$"
# A git ref may not start with "-" (argument injection) and may not contain the
# characters git itself forbids in refnames.
REF_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_./+-]{0,199}$"

Severity = Literal["P0", "P1", "P2", "P3"]
EvidenceStatus = Literal["CONFIRMED", "SUPPORTED", "HYPOTHESIS", "UNKNOWN", "REJECTED"]
#: How complete a report is. Named so workflows can declare it rather than
#: constructing a bare string that only fails at validation time.
ReportStatus = Literal["completed", "partial", "insufficient_context"]
ConfidenceBand = Literal["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
HypothesisState = Literal["open", "supported", "rejected", "confirmed"]
ProviderStatus = Literal[
    "AVAILABLE",
    "EMPTY",
    "UNAVAILABLE",
    "DENIED",
    "TIMEOUT",
    "NOT_CONFIGURED",
]
EvidenceKind = Literal[
    "code",
    "git",
    "jira",
    "image",
    "log",
    "metric",
    "trace",
    "sql",
    "test",
    "benchmark",
    "document",
]
RunState = Literal["queued", "running", "completed", "failed", "cancelled"]

#: Statuses that assert the evidence actually supports something. Reaching one
#: of these requires a successful retrieval; see :meth:`EvidenceItem.check`.
ASSERTIVE_STATUSES: frozenset[str] = frozenset({"CONFIRMED", "SUPPORTED", "REJECTED"})


class StorageUnavailable(RuntimeError):
    """DevLens cannot open or write its own database.

    Separate from ProviderError: nothing upstream failed, the deployment is
    wrong, and the message says which directory and which user.
    """


class ProviderError(Exception):
    """Sanitized upstream failure safe to expose to callers."""


class AccessDenied(Exception):
    """A policy or capability refused the operation."""


class Conflict(Exception):
    """The request cannot be applied to the current state of a resource."""


class CapacityReached(Exception):
    """A bounded resource is exhausted; the caller should retry later."""


def digest(value: Any) -> str:
    """Stable SHA-256 of any JSON-serialisable value."""
    encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------- #
# Provenance and evidence
# --------------------------------------------------------------------------- #


class Provenance(BaseModel):
    """Where a piece of evidence came from, and whether it was actually read.

    ``retrieved`` is the load-bearing field: it is ``True`` only when a real
    response came back from a real source. Every other field is descriptive.
    """

    retrieved: bool = False
    captured_at: float
    source: str = Field(min_length=1, max_length=400)
    provider: str | None = None
    provider_status: ProviderStatus | None = None
    repository: str | None = None
    commit: str | None = None
    ref: str | None = None
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    url: str | None = None
    query: str | None = None
    window_start: float | None = None
    window_end: float | None = None
    content_sha256: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Provenance:
        if self.retrieved and self.provider_status not in (None, "AVAILABLE", "EMPTY"):
            raise ValueError(
                "Provenance cannot claim retrieval while the provider reported "
                f"{self.provider_status}."
            )
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end < self.window_start
        ):
            raise ValueError("Provenance window ends before it starts.")
        return self


class EvidenceItem(BaseModel):
    """A single observation with its provenance and what it establishes.

    The validator is the enforcement point for DevLens's central claim: an item
    may not assert ``CONFIRMED``, ``SUPPORTED`` or ``REJECTED`` unless its
    provenance records a successful retrieval. Unreachable providers, empty
    result sets and denied queries all fall back to ``UNKNOWN``.
    """

    id: str = Field(min_length=1, max_length=64)
    type: EvidenceKind
    source: str = Field(min_length=1, max_length=400)
    observation: str = Field(min_length=1, max_length=8000)
    status: EvidenceStatus = "UNKNOWN"
    excerpt: str | None = Field(default=None, max_length=4000)
    selectors: list[str] = Field(default_factory=list, max_length=64)
    provenance: Provenance

    @model_validator(mode="after")
    def check(self) -> EvidenceItem:
        if self.status in ASSERTIVE_STATUSES and not self.provenance.retrieved:
            raise ValueError(
                f"Evidence {self.id} claims {self.status} without a successful "
                "retrieval. Unavailable or empty sources must stay UNKNOWN."
            )
        if self.type == "image" and self.status in {"CONFIRMED", "REJECTED"}:
            raise ValueError(
                "Image evidence cannot confirm or reject a claim on its own."
            )
        return self


class Hypothesis(BaseModel):
    """A candidate explanation, with the evidence for and against it.

    ``status`` is never set directly by a workflow. It is derived from the
    referenced evidence by :func:`devlens.evidence.settle`, so a model or a
    template cannot promote a hypothesis by assertion.
    """

    id: str = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=1, max_length=2000)
    status: HypothesisState = "open"
    confidence: ConfidenceBand = "LOW"
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    next_experiment: str | None = None

    @model_validator(mode="after")
    def check(self) -> Hypothesis:
        if self.status in {"supported", "confirmed"} and not self.supporting_evidence:
            raise ValueError(
                f"Hypothesis {self.id} is {self.status} with no supporting evidence."
            )
        if self.status == "rejected" and not self.contradicting_evidence:
            raise ValueError(
                f"Hypothesis {self.id} is rejected with no contradicting evidence."
            )
        if self.status == "confirmed" and self.confidence != "VERY_HIGH":
            raise ValueError("A confirmed hypothesis carries VERY_HIGH confidence.")
        if self.status == "open" and self.confidence not in {"LOW", "MEDIUM"}:
            raise ValueError("An open hypothesis cannot carry high confidence.")
        return self


class Finding(BaseModel):
    """Something DevLens believes is wrong, with its severity and support."""

    id: str = Field(min_length=1, max_length=64)
    severity: Severity
    status: EvidenceStatus
    confidence: ConfidenceBand
    category: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=300)
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    production_scenario: str | None = None
    evidence: list[str] = Field(default_factory=list)
    impact: str | None = None
    recommended_fix: str | None = None
    required_test: str | None = None
    verification: str | None = None

    @model_validator(mode="after")
    def check(self) -> Finding:
        if self.status == "CONFIRMED" and self.confidence != "VERY_HIGH":
            raise ValueError("A CONFIRMED finding carries VERY_HIGH confidence.")
        if self.status == "HYPOTHESIS" and self.confidence in {"HIGH", "VERY_HIGH"}:
            raise ValueError("A HYPOTHESIS finding cannot carry high confidence.")
        return self


# --------------------------------------------------------------------------- #
# Provider results
# --------------------------------------------------------------------------- #


class ProviderResult(BaseModel):
    """The outcome of one external query, with availability kept separate from content.

    Collapsing these three states into a list of strings is what let DevLens
    report a connection error as supporting evidence. They stay separate here.
    """

    provider: str
    status: ProviderStatus
    rows: list[str] = Field(default_factory=list, max_length=500)
    error: str | None = None
    query: str | None = None
    window_start: float | None = None
    window_end: float | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def check(self) -> ProviderResult:
        if self.status == "AVAILABLE" and not self.rows:
            raise ValueError("AVAILABLE requires at least one row; use EMPTY instead.")
        if self.status != "AVAILABLE" and self.rows:
            raise ValueError(f"{self.status} must not carry rows.")
        if self.status in {"UNAVAILABLE", "DENIED", "TIMEOUT"} and not self.error:
            raise ValueError(f"{self.status} requires an error reason.")
        return self

    @property
    def usable(self) -> bool:
        """True when a real response with content came back."""
        return self.status == "AVAILABLE"

    @property
    def reached(self) -> bool:
        """True when the provider answered at all, even with no rows."""
        return self.status in {"AVAILABLE", "EMPTY"}


# --------------------------------------------------------------------------- #
# Tool calls, runs and steps
# --------------------------------------------------------------------------- #


class ToolCall(BaseModel):
    """One recorded call into a provider or local tool.

    This is what makes a run reproducible and what the timeline is built from.
    ``arguments`` is redacted before it reaches here.
    """

    id: str
    run_id: str
    tool: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    started_at: float
    finished_at: float | None = None
    status: Literal["running", "ok", "error", "denied"] = "running"
    error: str | None = None
    result_digest: str | None = None
    result_bytes: int = 0

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at) * 1000)


class AgentEvent(BaseModel):
    """A progress event, emitted while work happens rather than replayed after."""

    ts: float
    kind: str = Field(min_length=1, max_length=60)
    message: str = Field(max_length=2000)
    data: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    """An investigation, distinct from the job that scheduled it.

    Carries everything needed to explain later why DevLens reached a conclusion.
    """

    id: str
    job_id: str | None = None
    command: str
    project: str
    status: RunState = "queued"
    devlens_version: str = DEVLENS_VERSION
    workflow_version: str = WORKFLOW_VERSION
    capabilities: list[str] = Field(default_factory=list)
    config_digest: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    repository: str | None = None
    commit: str | None = None
    requested_ref: str | None = None
    ticket_key: str | None = None
    ticket_snapshot_at: float | None = None
    started_at: float
    finished_at: float | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Proposals and approvals
# --------------------------------------------------------------------------- #

WriteAction = Literal[
    "create_remote_branch",
    "create_pr",
    "post_pr_comment",
    "post_jira_comment",
]
#: Actions DevLens will never perform, whatever the configuration says.
HUMAN_ONLY_ACTIONS: frozenset[str] = frozenset({"merge_pr", "deploy", "release"})


class Proposal(BaseModel):
    """A specific remote action a run wants to take, with its exact payload.

    An approval authorises one proposal. Because the payload is hashed at
    proposal time and re-checked at execution time, an approved action cannot
    be swapped for a different one.
    """

    id: str
    run_id: str
    action: WriteAction
    repository: str | None = Field(default=None, pattern=REPOSITORY_PATTERN)
    ticket_key: str | None = Field(default=None, pattern=TICKET_PATTERN)
    target: str = Field(min_length=1, max_length=400)
    payload: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=2000)
    created_at: float
    payload_sha256: str = ""

    @model_validator(mode="after")
    def seal(self) -> Proposal:
        computed = digest(
            {
                "action": self.action,
                "repository": self.repository,
                "ticket_key": self.ticket_key,
                "target": self.target,
                "payload": self.payload,
            }
        )
        if self.payload_sha256 and self.payload_sha256 != computed:
            raise ValueError("Proposal payload does not match its recorded hash.")
        object.__setattr__(self, "payload_sha256", computed)
        return self


class ApprovalRecord(BaseModel):
    """An operator decision about exactly one proposal."""

    id: str
    proposal_id: str
    run_id: str | None = None
    action: str
    target: str
    payload_sha256: str
    status: Literal["pending", "approving", "approved", "rejected", "expired"]
    requested_by: str = "operator"
    decided_by: str | None = None
    result: str | None = None
    created_at: float
    expires_at: float
    decided_at: float | None = None
    executed_at: float | None = None


class ApprovalCreate(BaseModel):
    """Request an approval for a proposal that already exists."""

    proposal_id: str = Field(min_length=1, max_length=64)


class ProposalCreate(BaseModel):
    """Operator-initiated proposal, used by the Implement surface."""

    action: WriteAction
    run_id: str = Field(min_length=1, max_length=64)
    repository: str | None = Field(default=None, pattern=REPOSITORY_PATTERN)
    ticket_key: str | None = Field(default=None, pattern=TICKET_PATTERN)
    target: str = Field(min_length=1, max_length=400)
    payload: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=2000)


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class AnalysisRequest(BaseModel):
    project: str = Field(default="default", pattern=PROJECT_PATTERN)
    ticket_key: str = Field(pattern=TICKET_PATTERN)
    repository: str = Field(pattern=REPOSITORY_PATTERN)
    ref: str = Field(default="HEAD", pattern=REF_PATTERN)


class TicketRequest(AnalysisRequest):
    pass


class ReviewRequest(BaseModel):
    project: str = Field(default="default", pattern=PROJECT_PATTERN)
    repository: str = Field(pattern=REPOSITORY_PATTERN)
    pr: int = Field(ge=1, le=10_000_000)
    ticket_key: str | None = Field(default=None, pattern=TICKET_PATTERN)
    run_tests: bool = False


class AnalyzeRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    query: str | None = Field(default=None, max_length=2000)


class ImplementRequest(BaseModel):
    project: str = Field(default="default", pattern=PROJECT_PATTERN)
    ticket_key: str = Field(pattern=TICKET_PATTERN)
    repository: str = Field(pattern=REPOSITORY_PATTERN)
    ref: str = Field(default="HEAD", pattern=REF_PATTERN)
    run_tests: bool = False


class PlatformRequest(BaseModel):
    project: str = Field(default="default", pattern=PROJECT_PATTERN)
    question: str | None = Field(default=None, max_length=4000)
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    ticket_key: str | None = Field(default=None, pattern=TICKET_PATTERN)
    repository: str | None = Field(default=None, pattern=REPOSITORY_PATTERN)
    ref: str = Field(default="HEAD", pattern=REF_PATTERN)
    pr: int | None = Field(default=None, ge=1, le=10_000_000)


# --------------------------------------------------------------------------- #
# Jira and git payloads
# --------------------------------------------------------------------------- #


class Ticket(BaseModel):
    key: str
    summary: str
    description: str
    url: str
    snapshot_at: float | None = None


class RepositoryFile(BaseModel):
    path: str
    size: int = Field(ge=0)


class JiraComment(BaseModel):
    id: str
    author: str | None = None
    created: str | None = None
    body: str


class Attachment(BaseModel):
    id: str
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    url: str
    sha256: str | None = None
    extracted_text: str | None = None
    image_analysis: str | None = None
    observations: list[str] = Field(default_factory=list)
    injection_suspected: bool = False


class LinkedIssue(BaseModel):
    key: str
    relation: str
    summary: str | None = None


class DevelopmentContext(BaseModel):
    branches: list[str] = Field(default_factory=list)
    commits: list[str] = Field(default_factory=list)
    pull_requests: list[str] = Field(default_factory=list)


class TicketSpec(BaseModel):
    key: str
    problem: str
    expected_behavior: str | None = None
    observed_behavior: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    important_comments: list[str] = Field(default_factory=list)
    attachment_observations: list[str] = Field(default_factory=list)
    probable_areas: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class Truncation(BaseModel):
    """What a paginated read did not see, recorded rather than hidden."""

    source: str
    fetched: int
    total: int | None = None
    reason: str

    def describe(self) -> str:
        total = f" of {self.total}" if self.total is not None else ""
        return f"{self.source}: inspected {self.fetched}{total} ({self.reason})."


class JiraIssueContext(BaseModel):
    key: str
    summary: str
    description: str
    url: str
    snapshot_at: float | None = None
    issue_type: str | None = None
    status: str | None = None
    priority: str | None = None
    reporter: str | None = None
    assignee: str | None = None
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    parent: str | None = None
    subtasks: list[str] = Field(default_factory=list)
    linked_issues: list[LinkedIssue] = Field(default_factory=list)
    fix_versions: list[str] = Field(default_factory=list)
    affected_versions: list[str] = Field(default_factory=list)
    custom_fields: dict[str, str] = Field(default_factory=dict)
    comments: list[JiraComment] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    development: DevelopmentContext | None = None
    truncations: list[Truncation] = Field(default_factory=list)
    spec: TicketSpec


class PullRequestFile(BaseModel):
    path: str
    status: str
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    binary: bool = False
    previous_path: str | None = None


class PullRequest(BaseModel):
    number: int
    title: str
    body: str
    source_branch: str
    target_branch: str
    head_sha: str
    base_sha: str | None = None
    state: str
    author: str | None = None
    url: str
    from_fork: bool = False
    files: list[PullRequestFile] = Field(default_factory=list)
    truncations: list[Truncation] = Field(default_factory=list)


class CommitInfo(BaseModel):
    sha: str
    message: str
    author: str | None = None


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


class AnalysisResult(BaseModel):
    run_id: str | None = None
    project: str = "default"
    provider: str = "github"
    status: Literal["completed", "insufficient_context"]
    method: Literal["lexical", "lexical+model"] = "lexical"
    devlens_version: str = DEVLENS_VERSION
    workflow_version: str = WORKFLOW_VERSION
    ticket: Ticket
    repository: str
    requested_ref: str | None = None
    commit: str
    summary: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    truncations: list[Truncation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class EngineeringReport(BaseModel):
    run_id: str | None = None
    task: str
    status: ReportStatus
    devlens_version: str = DEVLENS_VERSION
    workflow_version: str = WORKFLOW_VERSION
    executive_summary: str
    investigated: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    root_cause: str | None = None
    root_cause_status: EvidenceStatus = "UNKNOWN"
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    provider_results: list[ProviderResult] = Field(default_factory=list)
    timeline: list[AgentEvent] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    recommended_fix: str | None = None
    implementation_plan: list[str] = Field(default_factory=list)
    verification_plan: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    files_affected: list[str] = Field(default_factory=list)
    services_affected: list[str] = Field(default_factory=list)
    truncations: list[Truncation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    ticket: JiraIssueContext | None = None
    pull_request: PullRequest | None = None
    diff: str | None = None
    stage: str | None = None
    intent: str | None = None

    @model_validator(mode="after")
    def check(self) -> EngineeringReport:
        if self.root_cause is None and self.root_cause_status != "UNKNOWN":
            raise ValueError("A root cause status requires a root cause statement.")
        if self.root_cause_status == "CONFIRMED":
            confirmed = {
                item.id for item in self.evidence_items if item.status == "CONFIRMED"
            }
            if not confirmed:
                raise ValueError(
                    "A CONFIRMED root cause requires at least one CONFIRMED evidence item."
                )
        return self


# --------------------------------------------------------------------------- #
# Operator surfaces
# --------------------------------------------------------------------------- #


class IntentGuess(BaseModel):
    intent: str
    ticket_key: str | None = None
    pr: int | None = None
    repository: str | None = None
    question: str | None = None


class ProjectPublic(BaseModel):
    name: str
    repositories: list[str]
    jira_projects: list[str]
    git_provider: str


class JobCreate(BaseModel):
    command: Literal[
        "ticket",
        "analyze",
        "review",
        "implement",
        "ask",
        "investigate",
        "design",
        "observe",
    ]
    question: str | None = Field(default=None, max_length=4000)
    project: str = Field(default="default", pattern=PROJECT_PATTERN)
    ticket_key: str | None = Field(default=None, pattern=TICKET_PATTERN)
    repository: str | None = Field(default=None, pattern=REPOSITORY_PATTERN)
    ref: str = Field(default="HEAD", pattern=REF_PATTERN)
    pr: int | None = Field(default=None, ge=1, le=10_000_000)
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    query: str | None = Field(default=None, max_length=2000)
    run_tests: bool = False
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def safe_key(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise ValueError("Idempotency key must be URL-safe")
        return value


class JobRecord(BaseModel):
    id: str
    run_id: str | None = None
    command: str
    project: str
    status: RunState
    progress: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    error_kind: str | None = None
    created_at: float
    finished_at: float | None = None


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
