from devlens.agent.context_builder import build_context
from devlens.domain import AnalysisRequest, AnalysisResult
from devlens.tools.context import ContextTools


class TicketWorkflow:
    def __init__(self, tools: ContextTools):
        self.tools = tools

    async def run(self, request: AnalysisRequest) -> AnalysisResult:
        ticket = await self.tools.ticket(request)
        commit, evidence, limitations = await build_context(self.tools, request, ticket)
        return AnalysisResult(
            project=request.project,
            provider=self.tools.git.name,
            status="completed" if evidence else "insufficient_context",
            ticket=ticket,
            repository=request.repository,
            commit=commit,
            evidence=evidence,
            limitations=limitations,
            summary=f"Found {len(evidence)} relevant source lines for {ticket.key}."
            if evidence
            else "No matching source lines found within the inspection limits.",
        )
