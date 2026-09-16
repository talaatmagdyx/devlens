"""Ticket workflow: a Jira issue against one allowlisted repository."""

from __future__ import annotations

from devlens.agent.context_builder import build_context
from devlens.domain import AnalysisRequest, AnalysisResult
from devlens.runtime import RunContext
from devlens.tools.context import ContextTools


class TicketWorkflow:
    def __init__(self, tools: ContextTools):
        self.tools = tools

    async def run(
        self, request: AnalysisRequest, context: RunContext | None = None
    ) -> AnalysisResult:
        ticket = await self.tools.ticket(request, context=context)
        if context:
            context.observation(f"Read {ticket.key}: {ticket.summary[:120]}")
        commit, evidence, limitations, truncations = await build_context(
            self.tools, request, ticket, context=context
        )
        if context:
            context.observation(
                f"Ranked the change surface and collected {len(evidence)} matching lines"
            )
        return AnalysisResult(
            run_id=context.run_id if context else None,
            project=request.project,
            provider=self.tools.git.name,
            status="completed" if evidence else "insufficient_context",
            ticket=ticket,
            repository=request.repository,
            requested_ref=request.ref,
            commit=commit,
            evidence=evidence,
            truncations=truncations,
            limitations=limitations,
            summary=(
                f"Found {len(evidence)} source lines in {request.repository}@{commit[:12]} "
                f"that lexically match {ticket.key}."
                if evidence
                else "No source lines matched this ticket within the inspection limits."
            ),
        )
