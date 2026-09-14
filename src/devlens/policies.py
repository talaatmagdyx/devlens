from dataclasses import dataclass

from devlens.domain import AccessDenied, AnalysisRequest


@dataclass(frozen=True)
class ReadPolicy:
    repositories: frozenset[str]
    jira_projects: frozenset[str]

    def authorize(self, request: AnalysisRequest) -> None:
        if request.repository.lower() not in {
            repo.lower() for repo in self.repositories
        }:
            raise AccessDenied("Repository is not allowed.")
        if request.ticket_key.rsplit("-", 1)[0] not in self.jira_projects:
            raise AccessDenied("Jira project is not allowed.")
