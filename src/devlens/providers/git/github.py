import base64
import binascii
from urllib.parse import quote

import httpx

from devlens.domain import ProviderError, RepositoryFile
from devlens.providers.http import get_json


class GitHubProvider:
    name = "github"

    def evidence_url(self, repository: str, commit: str, path: str, line: int) -> str:
        return f"https://github.com/{repository}/blob/{commit}/{quote(path, safe='/')}#L{line}"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def resolve_ref(self, repository: str, ref: str) -> str:
        data = await get_json(
            self.client, f"repos/{repository}/commits/{quote(ref, safe='')}"
        )
        sha = data.get("sha")
        if (
            not isinstance(sha, str)
            or len(sha) != 40
            or any(c not in "0123456789abcdef" for c in sha)
        ):
            raise ProviderError("GitHub returned an invalid commit.")
        return sha

    async def list_files(self, repository: str, commit: str) -> list[RepositoryFile]:
        data = await get_json(
            self.client,
            f"repos/{repository}/git/trees/{commit}",
            params={"recursive": "1"},
        )
        if data.get("truncated"):
            raise ProviderError(
                "Repository tree exceeds the supported size; use a smaller repository."
            )
        try:
            return [
                RepositoryFile(path=item["path"], size=item.get("size", 0))
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
            if data.get("encoding") != "base64" or data.get("size", 0) > 100_000:
                raise ValueError()
            content = base64.b64decode("".join(data["content"].split()), validate=True)
            if len(content) > 100_000:
                raise ValueError()
            return content.decode("utf-8")
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise ProviderError(
                "Repository file is oversized, binary, or malformed."
            ) from None
