from typing import Protocol

from devlens.domain import JiraIssueContext, Ticket


class JiraProvider(Protocol):
    async def get_ticket(self, key: str) -> Ticket: ...
    async def get_issue_context(self, key: str) -> JiraIssueContext: ...
