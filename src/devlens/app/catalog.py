"""Operator catalog surfaces: services, knowledge, onboarding, search, settings.

The service catalog is **declared, not discovered**. Services come from the
``[services]`` table in ``devlens.toml`` and from repositories on the allowlist;
runs can add edges to what is already declared. Automatic discovery from a
running system remains unimplemented, and the capability inventory says so —
which is the honest version of a catalog that does exist and is useful.
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from devlens.domain import ApprovalRecord, JobRecord, ProviderError


class KnowledgeDocument(BaseModel):
    id: str
    category: str
    title: str
    body: str
    services: list[str] = Field(default_factory=list)
    related_runs: list[str] = Field(default_factory=list)


class KnowledgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)
    services: list[str] = Field(default_factory=list, max_length=50)
    related_runs: list[str] = Field(default_factory=list, max_length=50)


MAX_DOCUMENTS = 1000


class KnowledgeStore:
    def __init__(self):
        self.documents: list[KnowledgeDocument] = []

    def create(self, body: KnowledgeCreate) -> KnowledgeDocument:
        if len(self.documents) >= MAX_DOCUMENTS:
            raise ProviderError("Knowledge capacity reached.")
        record = KnowledgeDocument(id=str(uuid4()), **body.model_dump())
        self.documents.append(record)
        return record

    def list(self, category: str | None = None) -> list[KnowledgeDocument]:
        items = list(reversed(self.documents))
        return [item for item in items if item.category == category] if category else items

    def get(self, document_id: str) -> KnowledgeDocument | None:
        return next((item for item in self.documents if item.id == document_id), None)


class OnboardingStore:
    STEPS = (
        "connect_git",
        "select_repositories",
        "connect_jira",
        "select_jira_projects",
        "create_project",
        "set_analyze_root",
        "optional_observability",
        "first_investigation",
    )
    OPTIONAL = frozenset({"optional_observability"})

    def __init__(self):
        self.completed: list[str] = []

    def view(self, has_settings: bool, has_run: bool, has_root: bool = False) -> dict:
        done = set(self.completed)
        if has_settings:
            done.update(
                {
                    "connect_git",
                    "select_repositories",
                    "connect_jira",
                    "select_jira_projects",
                    "create_project",
                }
            )
        if has_root:
            done.add("set_analyze_root")
        if has_run:
            done.add("first_investigation")
        return {
            "steps": [{"id": step, "done": step in done} for step in self.STEPS],
            "complete": all(
                step in done or step in self.OPTIONAL for step in self.STEPS
            ),
        }

    def mark(self, step: str) -> dict:
        if step in self.STEPS and step not in self.completed:
            self.completed.append(step)
        return self.view(False, False)


def capacity(
    events_per_day: float, peak_multiplier: float = 10.0, payload_kb: float = 40.0
) -> dict:
    """Arithmetic, not estimation. Every number here is derived from the inputs."""
    daily = max(0.0, events_per_day)
    peak_x = max(1.0, peak_multiplier)
    payload = max(0.0, payload_kb)
    average = daily / 86_400 if daily else 0.0
    return {
        "events_per_day": daily,
        "average_per_sec": round(average, 3),
        "peak_multiplier": peak_x,
        "peak_per_sec": round(average * peak_x, 3),
        "payload_kb": payload,
        "daily_ingress_gb": round(daily * payload / 1024 / 1024, 3),
        "method": "arithmetic on the supplied inputs; no model is involved",
    }


def services(jobs: list[JobRecord], projects, declared: dict | None = None) -> list[dict]:
    """Merge declared services with repositories and observed boundaries."""
    found: dict[str, dict] = {}
    for name, entry in (declared or {}).items():
        found[name] = {
            "id": name,
            "name": name,
            "repository": getattr(entry, "repository", None),
            "provider": None,
            "project": None,
            "language": getattr(entry, "language", None),
            "framework": getattr(entry, "framework", None),
            "dependencies": list(getattr(entry, "dependencies", [])),
            "databases": list(getattr(entry, "databases", [])),
            "queues": list(getattr(entry, "queues", [])),
            "owners": list(getattr(entry, "owners", [])),
            "findings": 0,
            "origin": "declared",
        }
    for project in projects:
        for repo in project.repositories:
            name = repo.split("/")[-1]
            entry = found.setdefault(
                name,
                {
                    "id": name,
                    "name": name,
                    "dependencies": [],
                    "databases": [],
                    "queues": [],
                    "owners": [],
                    "findings": 0,
                    "origin": "repository",
                },
            )
            entry.setdefault("repository", repo)
            entry["repository"] = entry.get("repository") or repo
            entry["provider"] = project.git_provider
            entry["project"] = project.name
    for job in jobs:
        result = job.result or {}
        for name in result.get("services_affected") or []:
            entry = found.setdefault(
                name,
                {
                    "id": name,
                    "name": name,
                    "repository": None,
                    "provider": None,
                    "project": job.project,
                    "dependencies": [],
                    "databases": [],
                    "queues": [],
                    "owners": [],
                    "findings": 0,
                    "origin": "observed",
                },
            )
            entry["origin"] = entry.get("origin") or "observed"
        for finding in result.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            target = str(finding.get("file") or job.project)
            name = target.split("/")[0]
            entry = found.setdefault(
                name,
                {
                    "id": name,
                    "name": name,
                    "repository": None,
                    "provider": None,
                    "project": job.project,
                    "dependencies": [],
                    "databases": [],
                    "queues": [],
                    "owners": [],
                    "findings": 0,
                    "origin": "observed",
                },
            )
            entry["findings"] = entry.get("findings", 0) + 1
    return sorted(found.values(), key=lambda item: item["name"])


def notifications(jobs: list[JobRecord], approvals: list[ApprovalRecord]) -> list[dict]:
    items = []
    for approval in approvals:
        if approval.status == "pending":
            items.append(
                {
                    "id": approval.id,
                    "kind": "approval.requested",
                    "title": "Approval required",
                    "body": f"{approval.action} on {approval.target}",
                    "href": "/approvals",
                }
            )
    for job in jobs:
        if job.status == "completed":
            items.append(
                {
                    "id": job.id,
                    "kind": "run.completed",
                    "title": f"{job.command} completed",
                    "body": (job.result or {}).get("executive_summary") or job.command,
                    "href": f"/runs/{job.id}",
                }
            )
        elif job.status == "failed":
            items.append(
                {
                    "id": f"fail-{job.id}",
                    "kind": "run.failed",
                    "title": "Run failed",
                    "body": job.error or job.command,
                    "href": f"/runs/{job.id}",
                }
            )
    return items[:20]


def search(
    query: str,
    jobs: list[JobRecord],
    approvals: list[ApprovalRecord],
    knowledge: list[KnowledgeDocument],
    integrations: list[dict],
    service_items: list[dict],
) -> dict:
    needle = (query or "").strip().lower()
    groups: dict[str, list[dict]] = {
        "investigations": [],
        "prs": [],
        "runs": [],
        "knowledge": [],
        "services": [],
        "integrations": [],
        "approvals": [],
        "code": [],
    }
    if not needle:
        return {"query": query, "groups": groups}

    def hit(collection: str, title: str, href: str, kind: str, haystack: str = "") -> None:
        if needle in title.lower() or needle in kind.lower() or needle in haystack.lower():
            groups[collection].append({"title": title, "href": href, "kind": kind})

    for job in jobs:
        request = job.request or {}
        result = job.result or {}
        title = " ".join(
            str(part)
            for part in (job.command, request.get("ticket_key"), request.get("question"))
            if part
        )
        haystack = str(result.get("executive_summary") or "")
        href = f"/runs/{job.id}"
        hit("runs", f"{job.command} {title}", href, job.command, haystack)
        if job.command in {"investigate", "ticket", "observe", "analyze", "ask"}:
            hit("investigations", title, f"/investigations/{job.id}", job.command, haystack)
        if job.command == "review":
            hit("prs", title, f"/reviews/{job.id}", "review", haystack)
        for path in (result.get("files_affected") or [])[:200]:
            hit("code", str(path), href, "code")
    for document in knowledge:
        hit(
            "knowledge",
            f"{document.category} {document.title}",
            f"/knowledge/{document.id}",
            document.category,
            document.body,
        )
    for service in service_items:
        hit("services", service["name"], f"/services/{service['id']}", "service")
    for integration in integrations:
        hit(
            "integrations",
            integration["name"],
            "/integrations",
            integration.get("status", ""),
        )
    for approval in approvals:
        hit(
            "approvals",
            f"{approval.action} {approval.target}",
            "/approvals",
            approval.status,
        )
    for name in groups:
        groups[name] = groups[name][:25]
    return {"query": query, "groups": groups}


SETTINGS_SECTIONS = (
    "general",
    "members",
    "teams",
    "security",
    "models",
    "agent",
    "usage",
    "audit",
)


def settings_section(name: str, public: dict, audit: list) -> dict:
    if name not in SETTINGS_SECTIONS:
        raise KeyError(name)
    capabilities = public.get("capabilities") or {}
    if name == "general":
        return {
            "section": name,
            "projects": public.get("projects") or [],
            "analyze_root": public.get("analyze_root"),
            "version": public.get("version"),
        }
    if name == "security":
        return {
            "section": name,
            "sso": "unavailable",
            "mfa": "unavailable",
            "session": "cookie" if public.get("auth_required") else "open",
            "session_seconds": public.get("session_seconds"),
            "weak_password": public.get("weak_password"),
            "secrets": "Tokens are held server-side and are never sent to the browser.",
            "boundary": (
                "A single shared password with an attempt quota and expiring "
                "sessions. Adequate for one operator on a loopback interface; "
                "not an authentication boundary for a shared network."
            ),
        }
    if name == "models":
        return {
            "section": name,
            "default_model": public.get("model"),
            "provider": public.get("llm_provider"),
            "providers": ["openai", "claude", "codex", "claude_code", "codex_cli"],
            "auth": ["api_key", "oauth"],
            "enabled": "llm" in (capabilities.get("enabled") or []),
            "vision": "screenshot_analysis" in (capabilities.get("enabled") or []),
            "budget": public.get("model_budget"),
            "note": (
                "A model is used only when DEVLENS_LLM_PROVIDER is set. Vendor "
                "credentials in the environment do not enable one on their own."
            ),
        }
    if name == "agent":
        return {
            "section": name,
            "resilience": public.get("resilience"),
            "approval_policy": "required_for_writes",
            "execution_model": "in-process asyncio tasks, one worker",
            "capabilities": capabilities,
        }
    if name == "usage":
        return {
            "section": name,
            "model_budget": public.get("model_budget"),
            "note": (
                "Model call and character counts are measured locally. Token and "
                "currency costs are not, because DevLens does not see provider "
                "billing."
            ),
        }
    if name == "audit":
        return {"section": name, "events": audit[:20]}
    return {
        "section": name,
        "status": "unavailable",
        "note": "Members and teams are out of scope for this single-operator build.",
    }
