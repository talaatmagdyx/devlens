import httpx

from devlens.domain import ProviderError, Ticket
from devlens.providers.http import get_json


def adf_text(node) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "hardBreak":
        return "\n"
    text = node.get("text", "") + "".join(
        adf_text(child) for child in node.get("content", [])
    )
    return text + (
        "\n" if node.get("type") in {"paragraph", "heading", "listItem"} else ""
    )


class JiraCloudProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get_ticket(self, key: str) -> Ticket:
        data = await get_json(
            self.client,
            f"rest/api/3/issue/{key}",
            params={"fields": "summary,description"},
        )
        try:
            fields = data["fields"]
            return Ticket(
                key=key,
                summary=fields["summary"],
                description=adf_text(fields.get("description")).strip(),
                url=str(self.client.base_url.join(f"browse/{key}")),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError("Jira returned malformed ticket data.") from None
