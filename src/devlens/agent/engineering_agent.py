from devlens.agent.workflows.ticket import TicketWorkflow
from devlens.domain import AnalysisRequest, AnalysisResult


class EngineeringAgent:
    def __init__(self, ticket_workflow: TicketWorkflow):
        self.ticket_workflow = ticket_workflow

    async def analyze_ticket(self, request: AnalysisRequest) -> AnalysisResult:
        return await self.ticket_workflow.run(request)
