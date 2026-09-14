from typing import Protocol

from devlens.domain import Ticket


class JiraProvider(Protocol):
    async def get_ticket(self, key: str) -> Ticket: ...
