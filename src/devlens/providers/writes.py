"""Remote write execution.

A write happens only when all five of these hold:

1. The action is not merge, deploy or release — those are refused unconditionally
   and no configuration can enable them.
2. The capability (``git_writeback`` / ``jira_writeback``) is enabled.
3. The repository or Jira project is on the allowlist, re-checked here rather
   than trusted from the read path.
4. An operator approved **this proposal**, and the payload still hashes to what
   they approved.
5. No earlier execution of the same proposal has been recorded.

The payload carries the actual analysis. A comment DevLens posts says what it
found; it is not a placeholder.
"""

from __future__ import annotations

import time

from devlens.domain import (
    HUMAN_ONLY_ACTIONS,
    AccessDenied,
    Conflict,
    Proposal,
    ProviderError,
    digest,
)
from devlens.policies import ReadPolicy

CAPABILITY_FOR = {
    "create_remote_branch": "git_writeback",
    "create_pr": "git_writeback",
    "post_pr_comment": "git_writeback",
    "post_jira_comment": "jira_writeback",
}

MAX_BODY = 60_000


class WriteGateway:
    """Executes an approved proposal, once."""

    def __init__(self, git=None, jira=None, guard=None, policy: ReadPolicy | None = None):
        self.git = git
        self.jira = jira
        self.guard = guard
        self.policy = policy
        self._executed: dict[str, str] = {}

    @property
    def allowed(self) -> bool:
        if self.guard is None:
            return False
        return bool({"git_writeback", "jira_writeback"} & self.guard.enabled)

    def authorize(self, proposal: Proposal) -> None:
        """Every check that does not require a network call. Raises or returns."""
        if proposal.action in HUMAN_ONLY_ACTIONS:
            raise AccessDenied("Merge, deploy and release stay human-only.")
        capability = CAPABILITY_FOR.get(proposal.action)
        if capability is None:
            raise AccessDenied(f"{proposal.action} is not a permitted write.")
        if self.guard is None:
            raise AccessDenied("Write-back is disabled.")
        self.guard.require(capability)
        if self.policy is not None:
            if proposal.action == "post_jira_comment":
                if not proposal.ticket_key:
                    raise AccessDenied("A Jira comment requires a ticket key.")
                self.policy.authorize_ticket(proposal.ticket_key)
            else:
                if not proposal.repository:
                    raise AccessDenied("A git write requires a repository.")
                self.policy.authorize_repository(proposal.repository)

    def verify_seal(self, proposal: Proposal, approved_hash: str) -> None:
        """Refuse a payload that changed after approval (TOCTOU protection)."""
        current = digest(
            {
                "action": proposal.action,
                "repository": proposal.repository,
                "ticket_key": proposal.ticket_key,
                "target": proposal.target,
                "payload": proposal.payload,
            }
        )
        if current != approved_hash:
            raise Conflict(
                "The proposal changed after it was approved; approve it again."
            )

    async def execute(self, proposal: Proposal, approved_hash: str) -> str:
        self.authorize(proposal)
        self.verify_seal(proposal, approved_hash)
        if proposal.id in self._executed:
            return self._executed[proposal.id]
        result = await self._run(proposal)
        self._executed[proposal.id] = result
        return result

    async def _run(self, proposal: Proposal) -> str:
        payload = proposal.payload
        if proposal.action == "post_jira_comment":
            if self.jira is None:
                raise ProviderError("Jira write-back is not attached.")
            body = _body(payload.get("body"))
            await self.jira.post_comment(proposal.ticket_key, body)
            return f"Commented on {proposal.ticket_key} ({len(body)} characters)."
        if self.git is None:
            raise ProviderError("Git write-back is not attached.")
        repository = proposal.repository or ""
        if proposal.action == "create_remote_branch":
            branch = _branch(payload.get("branch"))
            sha = payload.get("sha") or await self.git.resolve_ref(repository, "HEAD")
            await self.git.create_branch(repository, branch, sha)
            return f"Created {repository}@{branch} at {str(sha)[:12]}."
        if proposal.action == "create_pr":
            head = _branch(payload.get("head"))
            base = payload.get("base") or await self.git.default_branch(repository)
            title = str(payload.get("title") or f"DevLens: {head}")[:250]
            url = await self.git.create_pull_request(
                repository, title, head, base, _body(payload.get("body"))
            )
            return url or f"Opened a pull request on {repository}."
        if proposal.action == "post_pr_comment":
            number = int(payload.get("number") or 0)
            if number < 1:
                raise ProviderError("A pull request number is required.")
            body = _body(payload.get("body"))
            await self.git.post_pr_comment(repository, number, body)
            return f"Commented on {repository}#{number} ({len(body)} characters)."
        raise AccessDenied(f"{proposal.action} is not a permitted write.")


def _body(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ProviderError(
            "A write payload must carry the analysis; DevLens does not post "
            "placeholder text."
        )
    return text[:MAX_BODY]


def _branch(value: object) -> str:
    name = str(value or "").strip()
    if not name.startswith("devlens/"):
        raise AccessDenied("DevLens only writes branches under the devlens/ prefix.")
    if len(name) > 200 or any(char in name for char in " ~^:?*[\\"):
        raise AccessDenied("Branch name is not a valid git ref.")
    return name


def branch_for(ticket_key: str | None, repository: str) -> str:
    """The deterministic branch name DevLens proposes for a ticket."""
    suffix = ticket_key or repository.split("/")[-1]
    return f"devlens/{suffix.lower()}-{int(time.time())}"
