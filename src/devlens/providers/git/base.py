from typing import Protocol

from devlens.domain import RepositoryFile


class GitProvider(Protocol):
    name: str

    def evidence_url(
        self, repository: str, commit: str, path: str, line: int
    ) -> str: ...

    async def resolve_ref(self, repository: str, ref: str) -> str: ...
    async def list_files(
        self, repository: str, commit: str
    ) -> list[RepositoryFile]: ...
    async def read_file(self, repository: str, commit: str, path: str) -> str: ...
