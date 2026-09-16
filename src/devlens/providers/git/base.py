"""The contract every git host adapter implements."""

from __future__ import annotations

from typing import Protocol

from devlens.domain import CommitInfo, PullRequest, PullRequestFile, RepositoryFile


class GitProvider(Protocol):
    name: str

    def evidence_url(self, repository: str, commit: str, path: str, line: int) -> str:
        """A permalink pinned to an immutable commit, never to a branch."""
        ...

    async def default_branch(self, repository: str) -> str: ...
    async def resolve_ref(self, repository: str, ref: str) -> str: ...
    async def list_files(self, repository: str, commit: str) -> list[RepositoryFile]: ...
    async def read_file(self, repository: str, commit: str, path: str) -> str: ...
    async def get_pull_request(self, repository: str, number: int) -> PullRequest: ...
    async def get_pull_request_files(
        self, repository: str, number: int
    ) -> tuple[list[PullRequestFile], bool]:
        """Files plus a flag saying whether pagination was cut short."""
        ...

    async def get_diff(self, repository: str, number: int) -> str: ...
    async def list_commits(
        self, repository: str, number: int
    ) -> tuple[list[CommitInfo], bool]: ...
