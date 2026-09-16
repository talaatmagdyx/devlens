"""Jira Cloud adapter.

Comments are paginated properly. The embedded ``comment`` field on an issue
returns only the first page, so a busy ticket used to be analysed from an
arbitrary slice of its discussion with nothing saying so. Here the dedicated
comment endpoint is followed to a cap, and whatever was not read is recorded as
a :class:`~devlens.domain.Truncation` on the issue.

Attachment downloads are bounded in count as well as size, and run through a
semaphore rather than serially.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

import httpx

from devlens.agent.ticket_spec import build_ticket_spec, display_name
from devlens.domain import (
    Attachment,
    JiraComment,
    JiraIssueContext,
    LinkedIssue,
    ProviderError,
    Ticket,
    Truncation,
)
from devlens.providers.attachments import process_attachment
from devlens.providers.http import get_bytes, get_json, request_json

ISSUE_FIELDS = (
    "summary,description,issuetype,status,priority,reporter,assignee,labels,"
    "components,parent,subtasks,issuelinks,fixVersions,versions,attachment"
)
COMMENT_PAGE = 100
MAX_COMMENTS = 300
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_BYTES = 2_000_000
ATTACHMENT_CONCURRENCY = 4


def adf_text(node) -> str:
    """Flatten Atlassian Document Format to plain text."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        return node.get("attrs", {}).get("text") or node.get("text") or ""
    if node_type == "emoji":
        return node.get("attrs", {}).get("shortName", "")
    if node_type == "inlineCard":
        return node.get("attrs", {}).get("url", "")
    if node_type == "codeBlock":
        body = "".join(adf_text(child) for child in node.get("content", []))
        return f"\n{body}\n"
    if node_type in {"table", "tableRow"}:
        return "".join(adf_text(child) for child in node.get("content", [])) + "\n"
    if node_type in {"tableCell", "tableHeader"}:
        return "".join(adf_text(child) for child in node.get("content", [])) + "\t"
    if node_type in {"bulletList", "orderedList"}:
        return "".join(adf_text(child) for child in node.get("content", []))
    if node_type == "media":
        return f"[attachment {node.get('attrs', {}).get('id', '')}]"
    text = node.get("text", "") + "".join(
        adf_text(child) for child in node.get("content", [])
    )
    if node_type in {"paragraph", "heading", "listItem", "blockquote"}:
        return text + "\n"
    return text


def _names(items) -> list[str]:
    return [
        str(item["name"])
        for item in items or []
        if isinstance(item, dict) and item.get("name")
    ]


def _custom_fields(fields: dict) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for key, value in fields.items():
        if not key.startswith("customfield_") or value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            extracted[key] = value[:4000]
        elif isinstance(value, dict):
            text = value.get("value") or value.get("name") or adf_text(value)
            if text:
                extracted[key] = str(text).strip()[:4000]
        elif isinstance(value, list):
            parts = [
                item if isinstance(item, str) else str(item.get("value") or item.get("name") or "")
                for item in value
                if isinstance(item, (str, dict))
            ]
            joined = ", ".join(part for part in parts if part)
            if joined:
                extracted[key] = joined[:4000]
    return extracted


class JiraCloudProvider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._slots = asyncio.Semaphore(ATTACHMENT_CONCURRENCY)

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
                snapshot_at=time.time(),
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError("Jira returned malformed ticket data.") from None

    async def get_issue_context(self, key: str) -> JiraIssueContext:
        snapshot = time.time()
        data = await get_json(
            self.client, f"rest/api/3/issue/{key}", params={"fields": ISSUE_FIELDS}
        )
        truncations: list[Truncation] = []
        try:
            fields = data["fields"]
            description = adf_text(fields.get("description")).strip()
            comments, comment_truncation = await self._comments(key)
            if comment_truncation:
                truncations.append(comment_truncation)
            attachments, attachment_truncation = await self._attachments(fields)
            if attachment_truncation:
                truncations.append(attachment_truncation)
            links = []
            for item in fields.get("issuelinks") or []:
                if not isinstance(item, dict):
                    continue
                relation = (item.get("type") or {}).get("name", "relates")
                target = item.get("outwardIssue") or item.get("inwardIssue")
                if isinstance(target, dict) and target.get("key"):
                    links.append(
                        LinkedIssue(
                            key=target["key"],
                            relation=relation,
                            summary=(target.get("fields") or {}).get("summary"),
                        )
                    )
            parent = fields.get("parent") or {}
            custom = _custom_fields(fields)
            spec = build_ticket_spec(
                key=key,
                summary=fields["summary"],
                description=description,
                comments=[item.body for item in comments],
                attachments=attachments,
                labels=list(fields.get("labels") or []),
                components=_names(fields.get("components")),
                custom_fields=custom,
            )
            return JiraIssueContext(
                key=key,
                summary=fields["summary"],
                description=description,
                url=str(self.client.base_url.join(f"browse/{key}")),
                snapshot_at=snapshot,
                issue_type=(fields.get("issuetype") or {}).get("name"),
                status=(fields.get("status") or {}).get("name"),
                priority=(fields.get("priority") or {}).get("name"),
                reporter=display_name(fields.get("reporter")),
                assignee=display_name(fields.get("assignee")),
                labels=list(fields.get("labels") or []),
                components=_names(fields.get("components")),
                parent=parent.get("key") if isinstance(parent, dict) else None,
                subtasks=[
                    item["key"]
                    for item in fields.get("subtasks") or []
                    if isinstance(item, dict) and item.get("key")
                ],
                linked_issues=links,
                fix_versions=_names(fields.get("fixVersions")),
                affected_versions=_names(fields.get("versions")),
                custom_fields=custom,
                comments=comments,
                attachments=attachments,
                truncations=truncations,
                spec=spec,
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError("Jira returned malformed ticket data.") from None

    async def _comments(self, key: str) -> tuple[list[JiraComment], Truncation | None]:
        """Page through the comment endpoint rather than trusting the issue field."""
        comments: list[JiraComment] = []
        start = 0
        total: int | None = None
        while len(comments) < MAX_COMMENTS:
            page = await get_json(
                self.client,
                f"rest/api/3/issue/{key}/comment",
                params={"startAt": start, "maxResults": COMMENT_PAGE, "orderBy": "created"},
            )
            total = int(page.get("total") or 0)
            batch = page.get("comments") or []
            for item in batch:
                if isinstance(item, dict):
                    comments.append(
                        JiraComment(
                            id=str(item.get("id", "")),
                            author=display_name(item.get("author")),
                            created=item.get("created"),
                            body=adf_text(item.get("body")).strip(),
                        )
                    )
            start += len(batch)
            if not batch or start >= total:
                break
        if total is not None and len(comments) < total:
            return comments, Truncation(
                source=f"{key} comments",
                fetched=len(comments),
                total=total,
                reason=f"capped at {MAX_COMMENTS}",
            )
        return comments, None

    async def _attachments(
        self, fields: dict
    ) -> tuple[list[Attachment], Truncation | None]:
        items = [
            item for item in (fields.get("attachment") or []) if isinstance(item, dict)
        ]
        selected = items[:MAX_ATTACHMENTS]
        results = await asyncio.gather(*(self._attachment(item) for item in selected))
        truncation = (
            Truncation(
                source="attachments",
                fetched=len(selected),
                total=len(items),
                reason=f"capped at {MAX_ATTACHMENTS} per ticket",
            )
            if len(items) > MAX_ATTACHMENTS
            else None
        )
        return list(results), truncation

    async def _attachment(self, item: dict) -> Attachment:
        identifier = str(item.get("id", ""))
        filename = str(item.get("filename", "attachment"))[:255]
        mime = str(item.get("mimeType") or "application/octet-stream")
        size = int(item.get("size") or 0)
        url = str(item.get("content") or "")
        content = None
        if identifier and self._allowed_url(url) and size <= MAX_ATTACHMENT_BYTES:
            try:
                async with self._slots:
                    content = await get_bytes(
                        self.client,
                        f"rest/api/3/attachment/content/{identifier}",
                        max_bytes=MAX_ATTACHMENT_BYTES,
                    )
            except ProviderError:
                content = None
        return process_attachment(
            identifier=identifier,
            filename=filename,
            mime_type=mime,
            size_bytes=size,
            url=url,
            content=content,
        )

    async def post_comment(self, key: str, body: str) -> None:
        """Post a comment, rendering paragraphs as separate ADF blocks."""
        paragraphs = [line for line in (body or "").split("\n\n") if line.strip()]
        content = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": paragraph[:30_000]}],
            }
            for paragraph in paragraphs
        ] or [{"type": "paragraph", "content": [{"type": "text", "text": body}]}]
        await request_json(
            self.client,
            "POST",
            f"rest/api/3/issue/{key}/comment",
            json={"body": {"type": "doc", "version": 1, "content": content}},
        )

    def _allowed_url(self, url: str) -> bool:
        """Only download attachments whose content URL is on the Jira site."""
        if not url:
            return False
        parsed = urlsplit(url)
        base = urlsplit(str(self.client.base_url))
        return parsed.scheme == "https" and parsed.hostname == base.hostname
