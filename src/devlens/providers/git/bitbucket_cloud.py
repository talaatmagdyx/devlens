"""Bitbucket Cloud REST adapter.

Bitbucket has no recursive tree endpoint, so listing a repository means walking
directories. The walk is breadth-first with bounded concurrency rather than one
sequential request per directory, and every pagination link is validated to stay
on the configured host before it is followed — an untrusted ``next`` URL is a
server-side request forgery primitive if it is not.

Nothing about GitHub is assumed: the default branch is resolved from
``mainbranch``, and file status values are normalised to a shared vocabulary.
"""

from __future__ import annotations

import asyncio
from collections import deque
from urllib.parse import quote

import httpx

from devlens.domain import (
    CommitInfo,
    ProviderError,
    PullRequest,
    PullRequestFile,
    RepositoryFile,
    Truncation,
)
from devlens.providers.http import get_bytes, get_json, paginate_values, request_json

MAX_DIRECTORIES = 500
WALK_CONCURRENCY = 8
PAGE_SIZE = 100
MAX_PAGES = 10
MAX_FILE_BYTES = 100_000
MAX_DIFF_BYTES = 2_000_000

#: Bitbucket's diffstat vocabulary mapped onto the shared one.
STATUS_MAP = {
    "added": "added",
    "removed": "removed",
    "modified": "modified",
    "renamed": "renamed",
    "merge conflict": "modified",
}


class BitbucketCloudProvider:
    name = "bitbucket_cloud"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._default_branch: dict[str, str] = {}

    def evidence_url(self, repository: str, commit: str, path: str, line: int) -> str:
        return (
            f"https://bitbucket.org/{repository}/src/{commit}/"
            f"{quote(path, safe='/')}#lines-{line}"
        )

    async def default_branch(self, repository: str) -> str:
        if repository not in self._default_branch:
            data = await get_json(self.client, f"repositories/{repository}")
            try:
                self._default_branch[repository] = data["mainbranch"]["name"]
            except (KeyError, TypeError):
                raise ProviderError(
                    "Bitbucket repository has no default branch."
                ) from None
        return self._default_branch[repository]

    async def resolve_ref(self, repository: str, ref: str) -> str:
        if ref == "HEAD":
            ref = await self.default_branch(repository)
        data = await get_json(
            self.client, f"repositories/{repository}/commit/{quote(ref, safe='')}"
        )
        sha = data.get("hash")
        if not _sha(sha):
            raise ProviderError("Bitbucket returned an invalid commit.")
        return str(sha)

    async def list_files(self, repository: str, commit: str) -> list[RepositoryFile]:
        root = f"repositories/{repository}/src/{commit}/"
        semaphore = asyncio.Semaphore(WALK_CONCURRENCY)
        files: dict[str, RepositoryFile] = {}
        visited: set[str] = set()
        frontier: deque[str] = deque([""])

        async def fetch(prefix: str) -> list[str]:
            async with semaphore:
                values, more = await paginate_values(
                    self.client,
                    root + (quote(prefix, safe="/") + "/" if prefix else ""),
                    params={"pagelen": PAGE_SIZE},
                    max_pages=MAX_PAGES,
                )
            if more:
                raise ProviderError(
                    f"Directory {prefix or '/'} exceeds the inspection limit."
                )
            children: list[str] = []
            for item in values:
                try:
                    path = item["path"]
                    kind = item["type"]
                except (KeyError, TypeError):
                    raise ProviderError("Bitbucket returned a malformed tree.") from None
                if not isinstance(path, str) or any(
                    part in {"", ".", ".."} for part in path.split("/")
                ):
                    raise ProviderError("Bitbucket returned an unsafe path.")
                if kind == "commit_directory":
                    children.append(path)
                elif kind == "commit_file" and not {
                    "link",
                    "subrepository",
                    "binary",
                }.intersection(item.get("attributes") or []):
                    files[path] = RepositoryFile(path=path, size=int(item.get("size") or 0))
            return children

        while frontier:
            if len(visited) > MAX_DIRECTORIES:
                raise ProviderError(
                    f"Repository exceeds the {MAX_DIRECTORIES}-directory inspection "
                    "limit; analyse a subdirectory instead."
                )
            batch = [frontier.popleft() for _ in range(min(len(frontier), WALK_CONCURRENCY))]
            fresh = [item for item in batch if item not in visited]
            visited.update(fresh)
            for children in await asyncio.gather(*(fetch(item) for item in fresh)):
                frontier.extend(child for child in children if child not in visited)
        return list(files.values())

    async def read_file(self, repository: str, commit: str, path: str) -> str:
        content = await get_bytes(
            self.client,
            f"repositories/{repository}/src/{commit}/{quote(path, safe='/')}",
            max_bytes=MAX_FILE_BYTES,
        )
        try:
            if b"\x00" in content:
                raise ValueError()
            return content.decode("utf-8")
        except (UnicodeError, ValueError):
            raise ProviderError("Bitbucket file is not supported text.") from None

    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        data = await get_json(
            self.client, f"repositories/{repository}/pullrequests/{number}"
        )
        try:
            source = data["source"]
            head = str((source.get("commit") or {}).get("hash") or "")
            if not _sha(head):
                raise ValueError()
            files, truncated = await self.get_pull_request_files(repository, number)
            source_repo = (source.get("repository") or {}).get("full_name")
            destination = data.get("destination") or {}
            truncations = []
            if truncated:
                truncations.append(
                    Truncation(
                        source=f"{repository}#{number} files",
                        fetched=len(files),
                        total=None,
                        reason=f"stopped after {MAX_PAGES} pages of {PAGE_SIZE}",
                    )
                )
            return PullRequest(
                number=int(data["id"]),
                title=data.get("title") or "",
                body=data.get("description") or "",
                source_branch=(source.get("branch") or {}).get("name") or "",
                target_branch=(destination.get("branch") or {}).get("name") or "",
                head_sha=head,
                base_sha=(destination.get("commit") or {}).get("hash"),
                state=data.get("state") or "",
                author=(data.get("author") or {}).get("display_name"),
                url=((data.get("links") or {}).get("html") or {}).get("href") or "",
                from_fork=bool(source_repo and source_repo != repository),
                files=files,
                truncations=truncations,
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError(
                "Bitbucket returned malformed pull request data."
            ) from None

    async def get_pull_request_files(
        self, repository: str, number: int
    ) -> tuple[list[PullRequestFile], bool]:
        values, more = await paginate_values(
            self.client,
            f"repositories/{repository}/pullrequests/{number}/diffstat",
            params={"pagelen": PAGE_SIZE},
            max_pages=MAX_PAGES,
        )
        try:
            files = []
            for item in values:
                new = item.get("new") or {}
                old = item.get("old") or {}
                path = new.get("path") or old.get("path")
                if not path:
                    raise ValueError()
                raw = str(item.get("status") or "modified").lower()
                files.append(
                    PullRequestFile(
                        path=path,
                        status=STATUS_MAP.get(raw, "modified"),
                        additions=int(item.get("lines_added") or 0),
                        deletions=int(item.get("lines_removed") or 0),
                        binary=bool(item.get("type") == "binary"),
                        previous_path=old.get("path") if raw == "renamed" else None,
                    )
                )
            return files, more
        except (KeyError, TypeError, ValueError):
            raise ProviderError(
                "Bitbucket returned malformed pull request files."
            ) from None

    async def get_diff(self, repository: str, number: int) -> str:
        content = await get_bytes(
            self.client,
            f"repositories/{repository}/pullrequests/{number}/diff",
            max_bytes=MAX_DIFF_BYTES,
        )
        try:
            return content.decode("utf-8")
        except UnicodeError:
            raise ProviderError(
                "Bitbucket returned a binary pull request diff."
            ) from None

    async def list_commits(
        self, repository: str, number: int
    ) -> tuple[list[CommitInfo], bool]:
        values, more = await paginate_values(
            self.client,
            f"repositories/{repository}/pullrequests/{number}/commits",
            params={"pagelen": PAGE_SIZE},
            max_pages=MAX_PAGES,
        )
        try:
            commits = []
            for item in values:
                sha = item.get("hash")
                if not _sha(sha):
                    raise ValueError()
                commits.append(
                    CommitInfo(
                        sha=sha,
                        message=item.get("message") or "",
                        author=(item.get("author") or {}).get("raw"),
                    )
                )
            return commits, more
        except (KeyError, TypeError, ValueError):
            raise ProviderError("Bitbucket returned malformed commits.") from None

    # -- writes ------------------------------------------------------------- #

    async def create_branch(self, repository: str, branch: str, sha: str) -> None:
        await request_json(
            self.client,
            "POST",
            f"repositories/{repository}/refs/branches",
            json={"name": branch, "target": {"hash": sha}},
        )

    async def create_pull_request(
        self, repository: str, title: str, head: str, base: str, body: str = ""
    ) -> str:
        data = await request_json(
            self.client,
            "POST",
            f"repositories/{repository}/pullrequests",
            json={
                "title": title,
                "description": body,
                "source": {"branch": {"name": head}},
                "destination": {"branch": {"name": base}},
            },
        )
        return str(((data.get("links") or {}).get("html") or {}).get("href") or "")

    async def post_pr_comment(self, repository: str, number: int, body: str) -> None:
        await request_json(
            self.client,
            "POST",
            f"repositories/{repository}/pullrequests/{number}/comments",
            json={"content": {"raw": body}},
        )


def _sha(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )
