"""Git host strategies.

Adding a provider is a table entry plus an adapter: nothing else in DevLens
needs to know the host exists. Authentication shape, API base, clone host and
token variable all travel together so a provider cannot be half-configured.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from devlens.providers.git.base import GitProvider
from devlens.providers.git.bitbucket_cloud import BitbucketCloudProvider
from devlens.providers.git.github import GitHubProvider
from devlens.providers.jira.cloud import JiraCloudProvider
from devlens.sandbox.workspace import CloneSettings


@dataclass(frozen=True)
class GitStrategy:
    api_url: str
    clone_host: str
    adapter: Callable[[httpx.AsyncClient], GitProvider]
    token_env: str
    headers: dict[str, str]
    basic_auth: bool = False

    def authorization(self, token: str, email: str = "") -> str:
        if self.basic_auth and email:
            encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
            return f"Basic {encoded}"
        return f"Bearer {token}"

    def client(self, token: str, email: str = "", **options) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_url,
            headers={
                **self.headers,
                "Authorization": self.authorization(token, email),
                "User-Agent": "devlens/0.2",
            },
            **options,
        )

    def clone(self, token: str, email: str = "") -> CloneSettings:
        return CloneSettings(self.clone_host, self.authorization(token, email))


GIT_STRATEGIES: dict[str, GitStrategy] = {
    "github": GitStrategy(
        "https://api.github.com/",
        "github.com",
        GitHubProvider,
        "DEVLENS_GITHUB_TOKEN",
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    ),
    "bitbucket_cloud": GitStrategy(
        "https://api.bitbucket.org/2.0/",
        "bitbucket.org",
        BitbucketCloudProvider,
        "DEVLENS_BITBUCKET_TOKEN",
        {"Accept": "application/json"},
        basic_auth=True,
    ),
}


def git_strategy(name: str) -> GitStrategy:
    try:
        return GIT_STRATEGIES[name]
    except KeyError:
        supported = ", ".join(sorted(GIT_STRATEGIES))
        raise ValueError(
            f"Unsupported git provider {name!r}; supported: {supported}."
        ) from None


class GitProviderFactory:
    @staticmethod
    def create(name: str, client: httpx.AsyncClient) -> GitProvider:
        return git_strategy(name).adapter(client)


class JiraProviderFactory:
    @staticmethod
    def create(client: httpx.AsyncClient) -> JiraCloudProvider:
        return JiraCloudProvider(client)
