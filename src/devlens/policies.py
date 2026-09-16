"""Authorization policy.

Two layers, both explicit:

:class:`ReadPolicy`
    Resource allowlists — which repositories and which Jira projects a project
    may touch. Enforced at the tool boundary and independently re-checked at the
    write boundary, so a new workflow cannot reach a resource by forgetting a
    check.

:class:`Policy`
    A decision over ``(actor, action, resource, environment)``. Capabilities
    answer "is this backend switched on"; this answers "may this actor do this
    to this resource, here". Today the actor set is one operator and the
    environment is one deployment, so the rules are short — but the decision is
    a value that can be logged and tested rather than an ``if`` in a route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from devlens.domain import HUMAN_ONLY_ACTIONS, AccessDenied


class AuthorizableRequest(Protocol):
    """Anything carrying the two resources a read policy governs."""

    @property
    def repository(self) -> str | None: ...

    @property
    def ticket_key(self) -> str | None: ...


Actor = Literal["operator", "viewer", "agent"]
Environment = Literal["local", "internal", "production"]

#: Actions that read. Available to every actor.
READ_ACTIONS = frozenset(
    {
        "jira.read",
        "repository.read",
        "pull_request.read",
        "workspace.read",
        "telemetry.read",
        "database.query",
        "run.read",
    }
)

#: Actions that change something outside DevLens. Operator only, and only with
#: an approved proposal.
WRITE_ACTIONS = frozenset(
    {
        "git.create_remote_branch",
        "git.create_pr",
        "git.post_pr_comment",
        "jira.post_comment",
    }
)

#: Actions that execute code from a repository under inspection.
EXECUTE_ACTIONS = frozenset({"repository.run_tests"})


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str

    def enforce(self) -> None:
        if not self.allow:
            raise AccessDenied(self.reason)


@dataclass(frozen=True)
class Policy:
    """Deny-by-default decisions over actor, action, resource and environment."""

    environment: Environment = "local"
    writes_enabled: bool = False
    execution_enabled: bool = False

    def decide(
        self,
        actor: Actor,
        action: str,
        resource: str = "",
        *,
        approved: bool = False,
    ) -> Decision:
        if action in HUMAN_ONLY_ACTIONS or action.rsplit(".", 1)[-1] in HUMAN_ONLY_ACTIONS:
            return Decision(False, "Merge, deploy and release stay human-only.")
        if action in READ_ACTIONS:
            if action == "database.query" and self.environment == "production":
                return Decision(
                    True,
                    "Read-only SELECT against production is permitted through the "
                    "gateway credential.",
                )
            return Decision(True, "Read access is permitted.")
        if action in EXECUTE_ACTIONS:
            if not self.execution_enabled:
                return Decision(
                    False,
                    "Executing repository code is disabled "
                    "(DEVLENS_ALLOW_REPO_TESTS).",
                )
            if self.environment == "production":
                return Decision(
                    False, "Repository code is never executed in a production environment."
                )
            return Decision(True, "Repository code execution is enabled for this host.")
        if action in WRITE_ACTIONS:
            if actor != "operator":
                return Decision(False, "Only an operator may authorise a remote write.")
            if not self.writes_enabled:
                return Decision(False, "Remote write-back is disabled (DEVLENS_ALLOW_WRITES).")
            if not approved:
                return Decision(
                    False, "This write has not been approved by an operator."
                )
            return Decision(True, f"Approved {action} on {resource or 'the target'}.")
        return Decision(False, f"Unknown action {action!r} is denied by default.")


@dataclass(frozen=True)
class ReadPolicy:
    """Which repositories and Jira projects one configured project may read."""

    repositories: frozenset[str]
    jira_projects: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_repositories", frozenset(r.lower() for r in self.repositories)
        )
        object.__setattr__(
            self, "_projects", frozenset(p.upper() for p in self.jira_projects)
        )

    def authorize(self, request: AuthorizableRequest) -> None:
        self.authorize_repository(request.repository)
        self.authorize_ticket(request.ticket_key)

    def authorize_repository(self, repository: str | None) -> None:
        if not repository or repository.lower() not in self._repositories:  # type: ignore[attr-defined]
            raise AccessDenied(
                f"Repository {repository!r} is not on this project's allowlist."
            )

    def authorize_ticket(self, ticket_key: str | None) -> None:
        if not ticket_key:
            raise AccessDenied("A Jira ticket key is required.")
        key = ticket_key.rsplit("-", 1)[0].upper()
        if key not in self._projects:  # type: ignore[attr-defined]
            raise AccessDenied(
                f"Jira project {key!r} is not on this project's allowlist."
            )
