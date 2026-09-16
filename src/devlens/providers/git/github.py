"""GitHub REST adapter.

Responses from GitHub are treated as untrusted input: every SHA is validated as
40 hexadecimal characters before it is used to build a permalink, a truncated
tree is refused rather than silently analysed in part, and file size is checked
both as declared and as decoded.

Pagination is followed and what was *not* fetched is reported, so a review of a
400-file pull request cannot quietly become a review of its first 100 files.
"""

from __future__ import annotations

import base64
import binascii
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
from devlens.providers.http import (
    get_bytes,
    get_json,
    paginate_link,
    request_json,
)

MAX_FILE_BYTES = 100_000
MAX_DIFF_BYTES = 2_000_000
PAGE_SIZE = 100
MAX_PAGES = 10


class GitHubProvider:
    name = "github"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._default_branch: dict[str, str] = {}

    def evidence_url(self, repository: str, commit: str, path: str, line: int) -> str:
        """A permalink pinned to an immutable commit SHA, never to a branch."""
        return (
            f"https://github.com/{repository}/blob/{commit}/"
            f"{quote(path, safe='/')}#L{line}"
        )

    async def default_branch(self, repository: str) -> str:
        if repository not in self._default_branch:
            data = await get_json(self.client, f"repos/{repository}")
            branch = data.get("default_branch")
            if not isinstance(branch, str) or not branch:
                raise ProviderError("GitHub did not report a default branch.")
            self._default_branch[repository] = branch
        return self._default_branch[repository]

    async def resolve_ref(self, repository: str, ref: str) -> str:
        data = await get_json(
            self.client, f"repos/{repository}/commits/{quote(ref, safe='')}"
        )
        sha = data.get("sha")
        if not _sha(sha):
            raise ProviderError("GitHub returned an invalid commit.")
        return str(sha)

    async def list_files(self, repository: str, commit: str) -> list[RepositoryFile]:
        data = await get_json(
            self.client,
            f"repos/{repository}/git/trees/{commit}",
            params={"recursive": "1"},
        )
        if data.get("truncated"):
            raise ProviderError(
                "Repository tree exceeds GitHub's response limit, so a partial "
                "analysis would be misleading. Analyse a subdirectory instead."
            )
        try:
            return [
                RepositoryFile(path=item["path"], size=int(item.get("size") or 0))
                for item in data["tree"]
                if item["type"] == "blob" and item.get("mode") in {"100644", "100755"}
            ]
        except (KeyError, TypeError, ValueError):
            raise ProviderError("GitHub returned a malformed tree.") from None

    async def read_file(self, repository: str, commit: str, path: str) -> str:
        data = await get_json(
            self.client,
            f"repos/{repository}/contents/{quote(path, safe='/')}",
            params={"ref": commit},
        )
        try:
            if data.get("encoding") != "base64" or int(data.get("size", 0)) > MAX_FILE_BYTES:
                raise ValueError()
            content = base64.b64decode("".join(data["content"].split()), validate=True)
            if len(content) > MAX_FILE_BYTES or b"\x00" in content:
                raise ValueError()
            return content.decode("utf-8")
        except (KeyError, TypeError, ValueError, binascii.Error, UnicodeDecodeError):
            raise ProviderError(
                "Repository file is oversized, binary, or malformed."
            ) from None

    async def get_pull_request(self, repository: str, number: int) -> PullRequest:
        data = await get_json(self.client, f"repos/{repository}/pulls/{number}")
        try:
            head = data["head"]["sha"]
            if not _sha(head):
                raise ValueError()
            files, truncated = await self.get_pull_request_files(repository, number)
            head_repo = (data.get("head") or {}).get("repo") or {}
            base_repo = (data.get("base") or {}).get("repo") or {}
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
                number=int(data["number"]),
                title=data.get("title") or "",
                body=data.get("body") or "",
                source_branch=data["head"]["ref"],
                target_branch=data["base"]["ref"],
                head_sha=head,
                base_sha=(data.get("base") or {}).get("sha"),
                state=data.get("state") or "",
                author=(data.get("user") or {}).get("login"),
                url=data.get("html_url") or "",
                from_fork=bool(
                    head_repo.get("full_name")
                    and head_repo.get("full_name") != base_repo.get("full_name")
                ),
                files=files,
                truncations=truncations,
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError("GitHub returned malformed pull request data.") from None

    async def get_pull_request_files(
        self, repository: str, number: int
    ) -> tuple[list[PullRequestFile], bool]:
        items, more = await paginate_link(
            self.client,
            f"repos/{repository}/pulls/{number}/files",
            params={"per_page": PAGE_SIZE},
            max_pages=MAX_PAGES,
        )
        try:
            files = [
                PullRequestFile(
                    path=item["filename"],
                    status=item.get("status") or "modified",
                    additions=int(item.get("additions") or 0),
                    deletions=int(item.get("deletions") or 0),
                    binary=item.get("patch") is None
                    and item.get("status") not in {"removed", "renamed"},
                    previous_path=item.get("previous_filename"),
                )
                for item in items
                if isinstance(item, dict)
            ]
        except (KeyError, TypeError, ValueError):
            raise ProviderError("GitHub returned malformed pull request files.") from None
        return files, more

    async def get_diff(self, repository: str, number: int) -> str:
        content = await get_bytes(
            self.client,
            f"repos/{repository}/pulls/{number}",
            headers={"Accept": "application/vnd.github.diff"},
            max_bytes=MAX_DIFF_BYTES,
        )
        try:
            return content.decode("utf-8")
        except UnicodeError:
            raise ProviderError("GitHub returned a binary pull request diff.") from None

    async def list_commits(
        self, repository: str, number: int
    ) -> tuple[list[CommitInfo], bool]:
        items, more = await paginate_link(
            self.client,
            f"repos/{repository}/pulls/{number}/commits",
            params={"per_page": PAGE_SIZE},
            max_pages=MAX_PAGES,
        )
        try:
            commits = []
            for item in items:
                sha = item["sha"]
                if not _sha(sha):
                    raise ValueError()
                commit = item.get("commit") or {}
                commits.append(
                    CommitInfo(
                        sha=sha,
                        message=commit.get("message") or "",
                        author=(commit.get("author") or {}).get("name"),
                    )
                )
            return commits, more
        except (KeyError, TypeError, ValueError):
            raise ProviderError("GitHub returned malformed commits.") from None

    # -- writes ------------------------------------------------------------- #

    async def create_branch(self, repository: str, branch: str, sha: str) -> None:
        await request_json(
            self.client,
            "POST",
            f"repos/{repository}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    async def create_pull_request(
        self, repository: str, title: str, head: str, base: str, body: str = ""
    ) -> str:
        data = await request_json(
            self.client,
            "POST",
            f"repos/{repository}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        return str(data.get("html_url") or "")

    async def post_pr_comment(self, repository: str, number: int, body: str) -> None:
        await request_json(
            self.client,
            "POST",
            f"repos/{repository}/issues/{number}/comments",
            json={"body": body},
        )


def _sha(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )
