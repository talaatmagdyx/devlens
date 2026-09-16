"""Workflow runner.

Named for what it is. There is no planner, no tool-selection loop and no model
in the control path — DevLens runs fixed, auditable workflows and the language
model, when enabled, only summarises evidence that was already retrieved. That
is a deliberate design choice for a tool whose output is meant to be checkable,
and calling it an "agent" would overstate it.

What this class adds around a workflow: capability limitations merged into every
report, proposals persisted so they can be approved later, and an audit record
of what ran and how long it took.
"""

from __future__ import annotations

import time

from devlens.agent.audit import AuditLog
from devlens.agent.workflows.analyze import AnalyzeWorkflow
from devlens.agent.workflows.implementation import ImplementWorkflow
from devlens.agent.workflows.platform import (
    AskWorkflow,
    DesignWorkflow,
    InvestigateWorkflow,
    ObserveWorkflow,
)
from devlens.agent.workflows.review import ReviewWorkflow
from devlens.agent.workflows.ticket import TicketWorkflow
from devlens.domain import (
    AnalysisRequest,
    AnalysisResult,
    AnalyzeRequest,
    EngineeringReport,
    ImplementRequest,
    PlatformRequest,
    ProviderError,
    ReviewRequest,
)
from devlens.guardrails import CapabilityGuard
from devlens.runtime import RunContext


class WorkflowRunner:
    """Dispatches to one fixed workflow and records what happened."""

    def __init__(
        self,
        ticket_workflow: TicketWorkflow,
        review_workflow: ReviewWorkflow | None = None,
        implement_workflow: ImplementWorkflow | None = None,
        analyze_workflow: AnalyzeWorkflow | None = None,
        audit: AuditLog | None = None,
        guard: CapabilityGuard | None = None,
        approvals=None,
        tools=None,
    ):
        self.capabilities = guard or CapabilityGuard()
        self.ticket_workflow = ticket_workflow
        self.review_workflow = review_workflow
        self.implement_workflow = implement_workflow
        self.analyze_workflow = analyze_workflow or AnalyzeWorkflow()
        self.ask_workflow = AskWorkflow(self.capabilities, tools=tools)
        self.investigate_workflow = InvestigateWorkflow(self.capabilities, tools=tools)
        self.design_workflow = DesignWorkflow(self.capabilities, tools=tools)
        self.observe_workflow = ObserveWorkflow(self.capabilities, tools=tools)
        self.audit = audit
        self.approvals = approvals

    async def analyze_ticket(
        self, request: AnalysisRequest, context: RunContext | None = None
    ) -> AnalysisResult:
        return await self._traced("ticket", request, self.ticket_workflow.run, context)

    async def review(
        self, request: ReviewRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        if self.review_workflow is None:
            raise ProviderError("The review workflow is not configured.")
        return await self._traced("review", request, self.review_workflow.run, context)

    async def implement(
        self, request: ImplementRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        if self.implement_workflow is None:
            raise ProviderError("The implementation workflow is not configured.")
        return await self._traced(
            "implement", request, self.implement_workflow.run, context
        )

    async def analyze(
        self, request: AnalyzeRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        return await self._traced("analyze", request, self.analyze_workflow.run, context)

    async def ask(
        self, request: PlatformRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        return await self._traced("ask", request, self.ask_workflow.run, context)

    async def investigate(
        self, request: PlatformRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        return await self._traced(
            "investigate", request, self.investigate_workflow.run, context
        )

    async def design(
        self, request: PlatformRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        return await self._traced("design", request, self.design_workflow.run, context)

    async def observe(
        self, request: PlatformRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        return await self._traced("observe", request, self.observe_workflow.run, context)

    async def _traced(self, command, request, runner, context: RunContext | None):
        started = time.monotonic()
        payload = {
            "event": "run.started",
            "command": command,
            "run_id": context.run_id if context else None,
            "project": getattr(request, "project", None),
            "ticket": getattr(request, "ticket_key", None),
            "repository": getattr(request, "repository", None),
            "path": getattr(request, "path", None),
            "capabilities": sorted(self.capabilities.enabled),
        }
        try:
            result = await runner(request, context=context)
            self._merge_limitations(result)
            self._persist_proposals(result)
            self._record(
                {
                    **payload,
                    "event": "run.completed",
                    "status": getattr(result, "status", None),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "tool_calls": len(context.calls) if context else 0,
                }
            )
            return result
        except Exception as exc:
            self._record(
                {
                    **payload,
                    "event": "run.failed",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:300],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            )
            raise

    def _merge_limitations(self, result) -> None:
        limitations = getattr(result, "limitations", None)
        if isinstance(limitations, list):
            present = set(limitations)
            limitations.extend(
                item for item in self.capabilities.limitations() if item not in present
            )

    def _persist_proposals(self, result) -> None:
        """Store any proposals so an operator can approve one later."""
        proposals = getattr(result, "proposals", None)
        if not proposals or self.approvals is None:
            return
        for proposal in proposals:
            self.approvals.propose(proposal)

    def _record(self, event: dict) -> None:
        if self.audit is not None:
            self.audit.record(event)


#: Retained so existing imports keep working; the name overstated what it does.
EngineeringAgent = WorkflowRunner
