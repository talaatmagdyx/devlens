from collections import deque
from urllib.parse import quote

import httpx

from devlens.domain import ProviderError, RepositoryFile
from devlens.providers.http import get_bytes, get_json


class BitbucketCloudProvider:
    name = "bitbucket_cloud"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    def evidence_url(self, repository: str, commit: str, path: str, line: int) -> str:
        return f"https://bitbucket.org/{repository}/src/{commit}/{quote(path, safe='/')}#lines-{line}"

    async def resolve_ref(self, repository: str, ref: str) -> str:
        if ref == "HEAD":
            data = await get_json(self.client, f"repositories/{repository}")
            try:
                ref = data["mainbranch"]["name"]
            except (KeyError, TypeError):
                raise ProviderError(
                    "Bitbucket repository has no default branch."
                ) from None
        data = await get_json(
            self.client, f"repositories/{repository}/commit/{quote(ref, safe='')}"
        )
        sha = data.get("hash")
        if (
            not isinstance(sha, str)
            or len(sha) != 40
            or any(c not in "0123456789abcdef" for c in sha)
        ):
            raise ProviderError("Bitbucket returned an invalid commit.")
        return sha

    async def list_files(self, repository: str, commit: str) -> list[RepositoryFile]:
        root = f"repositories/{repository}/src/{commit}/"
        pending = deque([str(self.client.base_url.join(root)) + "?pagelen=100"])
        visited = set()
        files = {}
        while pending:
            url = pending.popleft()
            if url in visited:
                raise ProviderError("Bitbucket returned cyclic pagination.")
            visited.add(url)
            if len(visited) > 500:
                raise ProviderError(
                    "Bitbucket repository exceeds the 500-page inspection limit."
                )
            data = await get_json(self.client, url)
            try:
                for item in data["values"]:
                    path = item["path"]
                    if not isinstance(path, str) or any(
                        p in {"", ".", ".."} for p in path.split("/")
                    ):
                        raise ValueError()
                    if item["type"] == "commit_directory":
                        pending.append(
                            str(
                                self.client.base_url.join(
                                    root + quote(path, safe="/") + "/"
                                )
                            )
                            + "?pagelen=100"
                        )
                    elif item["type"] == "commit_file" and not {
                        "link",
                        "subrepository",
                        "binary",
                    }.intersection(item.get("attributes", [])):
                        files[path] = RepositoryFile(path=path, size=item["size"])
                if data.get("next"):
                    next_url = httpx.URL(data["next"])
                    base = self.client.base_url
                    if (
                        next_url.username
                        or next_url.password
                        or next_url.fragment
                        or (next_url.scheme, next_url.host, next_url.port)
                        != (base.scheme, base.host, base.port)
                        or not next_url.path.startswith(base.join(root).path)
                    ):
                        raise ValueError()
                    pending.append(str(next_url))
            except (KeyError, TypeError, ValueError):
                raise ProviderError(
                    "Bitbucket returned malformed or unsafe tree pagination."
                ) from None
        return list(files.values())

    async def read_file(self, repository: str, commit: str, path: str) -> str:
        content = await get_bytes(
            self.client,
            f"repositories/{repository}/src/{commit}/{quote(path, safe='/')}",
            max_bytes=100_000,
        )
        try:
            if b"\x00" in content:
                raise ValueError()
            return content.decode("utf-8")
        except (UnicodeError, ValueError):
            raise ProviderError("Bitbucket file is not supported text.") from None
