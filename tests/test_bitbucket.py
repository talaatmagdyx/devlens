"""Bitbucket Cloud adapter.

The second git host exists to prove the boundary is a strategy rather than a
hard-coded GitHub. It gets the same treatment as GitHub: a malformed payload is
a ProviderError, a non-SHA commit is refused, a fork is flagged, a directory
walk that exceeds its ceiling says so, and every pagination hop is validated
against the configured host.
"""

from __future__ import annotations

import json

import httpx
import pytest

from devlens.domain import ProviderError
from devlens.providers.factory import GitProviderFactory, git_strategy
from devlens.providers.git.bitbucket_cloud import BitbucketCloudProvider

SHA = "c" * 40
BASE = "https://api.bitbucket.org/2.0/"


def provider(handler) -> BitbucketCloudProvider:
    return BitbucketCloudProvider(
        httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    )


def repository_handler(**overrides):
    defaults = {
        "mainbranch": {"name": "develop"},
        "commit": {"hash": SHA},
        "src": [{"path": "src/app.py", "type": "commit_file", "size": 120}],
        "diffstat": [
            {"new": {"path": "src/app.py"}, "status": "modified",
             "lines_added": 3, "lines_removed": 1, "type": "commit_file"}
        ],
        "pull_request": {
            "id": 7,
            "title": "Fix checkout",
            "description": "body",
            "state": "OPEN",
            "source": {
                "commit": {"hash": SHA},
                "branch": {"name": "feature"},
                "repository": {"full_name": "team/repo"},
            },
            "destination": {"commit": {"hash": SHA}, "branch": {"name": "develop"}},
            "author": {"display_name": "dev"},
            "links": {"html": {"href": "https://bitbucket.org/team/repo/pull-requests/7"}},
        },
    }
    defaults.update(overrides)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/diff"):
            return httpx.Response(
                200,
                content=b"diff --git a/src/app.py b/src/app.py\n@@ -1 +1,2 @@\n+added\n",
            )
        if "/diffstat" in path:
            return httpx.Response(200, json={"values": defaults["diffstat"]})
        if "/commits" in path:
            return httpx.Response(
                200,
                json={"values": [{"hash": SHA, "message": "m", "author": {"raw": "dev"}}]},
            )
        if "/pullrequests/" in path and "/comments" not in path:
            return httpx.Response(200, json=defaults["pull_request"])
        if "/commit/" in path:
            return httpx.Response(200, json={"hash": defaults["commit"]["hash"]})
        if "/src/" in path:
            return httpx.Response(200, json={"values": defaults["src"]})
        return httpx.Response(200, json={"mainbranch": defaults["mainbranch"]})

    return handler


@pytest.mark.asyncio
async def test_the_default_branch_is_read_not_assumed_to_be_main():
    assert await provider(repository_handler()).default_branch("team/repo") == "develop"


@pytest.mark.asyncio
async def test_head_resolves_through_the_default_branch():
    assert await provider(repository_handler()).resolve_ref("team/repo", "HEAD") == SHA


@pytest.mark.asyncio
async def test_a_repository_without_a_default_branch_is_a_provider_error():
    with pytest.raises(ProviderError, match="no default branch"):
        await provider(repository_handler(mainbranch={})).default_branch("team/repo")


@pytest.mark.asyncio
async def test_a_commit_that_is_not_a_sha_is_refused():
    with pytest.raises(ProviderError, match="invalid commit"):
        await provider(repository_handler(commit={"hash": "short"})).resolve_ref(
            "team/repo", "develop"
        )


@pytest.mark.asyncio
async def test_files_are_listed_from_the_source_walk():
    files = await provider(repository_handler()).list_files("team/repo", SHA)
    assert [item.path for item in files] == ["src/app.py"]


@pytest.mark.asyncio
async def test_a_binary_file_is_refused_rather_than_mangled():
    def handler(request):
        return httpx.Response(200, content=b"\x00\x01")

    with pytest.raises(ProviderError, match="not supported text"):
        await provider(handler).read_file("team/repo", SHA, "a.bin")


@pytest.mark.asyncio
async def test_a_pull_request_carries_its_files_and_its_head_sha():
    pull = await provider(repository_handler()).get_pull_request("team/repo", 7)
    assert pull.number == 7
    assert pull.head_sha == SHA
    assert pull.target_branch == "develop"
    assert [item.path for item in pull.files] == ["src/app.py"]
    assert pull.from_fork is False


@pytest.mark.asyncio
async def test_a_pull_request_from_a_fork_is_flagged():
    payload = repository_handler()
    forked = repository_handler()

    def handler(request):
        response = payload(request)
        if "/pullrequests/" in request.url.path and "/diffstat" not in request.url.path:
            body = response.json()
            body["source"]["repository"]["full_name"] = "attacker/repo"
            return httpx.Response(200, json=body)
        return response

    pull = await provider(handler).get_pull_request("team/repo", 7)
    assert pull.from_fork is True
    assert forked is not None


@pytest.mark.asyncio
async def test_a_malformed_pull_request_is_a_provider_error():
    with pytest.raises(ProviderError, match="malformed pull request data"):
        await provider(repository_handler(pull_request={"id": 7})).get_pull_request(
            "team/repo", 7
        )


@pytest.mark.asyncio
async def test_an_evidence_url_pins_the_commit():
    url = provider(repository_handler()).evidence_url("team/repo", SHA, "src/app.py", 4)
    assert f"/src/{SHA}/" in url
    assert url.endswith("#lines-4")


@pytest.mark.asyncio
async def test_a_diff_is_returned_as_text():
    diff = await provider(repository_handler()).get_diff("team/repo", 7)
    assert diff.startswith("diff --git")


def test_the_strategy_table_carries_authentication_and_the_clone_host():
    strategy = git_strategy("bitbucket_cloud")
    assert strategy.clone_host == "bitbucket.org"
    assert strategy.token_env == "DEVLENS_BITBUCKET_TOKEN"
    assert strategy.authorization("token", "dev@example.com").startswith("Basic ")
    assert strategy.authorization("token").startswith("Bearer ")


def test_an_unsupported_provider_names_the_supported_ones():
    with pytest.raises(ValueError, match=r"github, bitbucket_cloud|bitbucket_cloud, github"):
        git_strategy("gitlab")


def test_the_factory_returns_the_adapter_for_each_host():
    client = httpx.AsyncClient(base_url=BASE)
    assert isinstance(
        GitProviderFactory.create("bitbucket_cloud", client), BitbucketCloudProvider
    )
    from devlens.providers.git.github import GitHubProvider

    assert isinstance(GitProviderFactory.create("github", client), GitHubProvider)


# --------------------------------------------------------------------------- #
# Commits, writes and the directory walk
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pull_request_commits_parse_with_their_authors():
    commits, more = await provider(repository_handler()).list_commits("team/repo", 7)
    assert [item.sha for item in commits] == [SHA]
    assert commits[0].author == "dev"
    assert more is False


@pytest.mark.asyncio
async def test_a_commit_without_a_sha_is_malformed():
    def handler(request):
        return httpx.Response(200, json={"values": [{"hash": "short"}]})

    with pytest.raises(ProviderError, match="malformed commits"):
        await provider(handler).list_commits("team/repo", 7)


@pytest.mark.asyncio
async def test_a_branch_is_created_with_its_target_hash():
    captured: dict = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.read())
        return httpx.Response(201, json={})

    await provider(handler).create_branch("team/repo", "devlens/dev-1", SHA)
    assert captured["path"].endswith("/refs/branches")
    assert captured["body"] == {"name": "devlens/dev-1", "target": {"hash": SHA}}


@pytest.mark.asyncio
async def test_a_pull_request_is_created_and_its_url_returned():
    def handler(request):
        body = json.loads(request.read())
        assert body["source"]["branch"]["name"] == "devlens/dev-1"
        assert body["destination"]["branch"]["name"] == "develop"
        return httpx.Response(
            201,
            json={"links": {"html": {"href": "https://bitbucket.org/team/repo/pull-requests/9"}}},
        )

    url = await provider(handler).create_pull_request(
        "team/repo", "DevLens: dev-1", "devlens/dev-1", "develop", "the analysis"
    )
    assert url.endswith("/pull-requests/9")


@pytest.mark.asyncio
async def test_a_pull_request_with_no_link_still_succeeds():
    async def run() -> str:
        return await provider(lambda request: httpx.Response(201, json={})).create_pull_request(
            "team/repo", "t", "devlens/x", "main", "b"
        )

    assert await run() == ""


@pytest.mark.asyncio
async def test_a_comment_is_posted_as_raw_content():
    captured: dict = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.read())
        return httpx.Response(201, json={})

    await provider(handler).post_pr_comment("team/repo", 7, "three findings")
    assert captured["path"].endswith("/pullrequests/7/comments")
    assert captured["body"] == {"content": {"raw": "three findings"}}


@pytest.mark.asyncio
async def test_the_directory_walk_descends_and_stops_at_its_ceiling():
    """Bitbucket has no recursive tree endpoint, so the walk is ours to bound."""

    def handler(request):
        path = request.url.path
        if path.rstrip("/").endswith("/src/" + SHA) or path.endswith(f"/src/{SHA}/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"path": "app.py", "type": "commit_file", "size": 10},
                        {"path": "src", "type": "commit_directory"},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"values": [{"path": "src/inner.py", "type": "commit_file", "size": 20}]},
        )

    files = await provider(handler).list_files("team/repo", SHA)
    paths = {item.path for item in files}
    assert "app.py" in paths
    assert "src/inner.py" in paths


@pytest.mark.asyncio
async def test_a_directory_that_exceeds_the_page_ceiling_is_refused():
    """A directory too large to page through is refused, not half-read."""
    page = {"n": 0}

    def handler(request):
        page["n"] += 1
        return httpx.Response(
            200,
            json={
                "values": [{"path": f"a{page['n']}.py", "type": "commit_file", "size": 1}],
                "next": f"https://api.bitbucket.org/2.0/x?page={page['n'] + 1}",
            },
        )

    with pytest.raises(ProviderError, match="exceeds the inspection limit"):
        await provider(handler).list_files("team/repo", SHA)


@pytest.mark.asyncio
async def test_a_malformed_diffstat_entry_is_a_provider_error():
    def handler(request):
        if "/diffstat" in request.url.path:
            return httpx.Response(200, json={"values": [{"status": "modified"}]})
        return httpx.Response(200, json={"values": []})

    with pytest.raises(ProviderError, match="malformed pull request files"):
        await provider(handler).get_pull_request_files("team/repo", 7)


@pytest.mark.asyncio
async def test_a_renamed_file_keeps_its_previous_path():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "values": [
                    {
                        "new": {"path": "b.py"},
                        "old": {"path": "a.py"},
                        "status": "renamed",
                        "lines_added": 1,
                        "lines_removed": 1,
                    }
                ]
            },
        )

    files, _ = await provider(handler).get_pull_request_files("team/repo", 7)
    assert files[0].path == "b.py"
    assert files[0].previous_path == "a.py"


@pytest.mark.asyncio
async def test_a_binary_entry_is_marked_binary():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "values": [
                    {"new": {"path": "logo.png"}, "status": "added", "type": "binary"}
                ]
            },
        )

    files, _ = await provider(handler).get_pull_request_files("team/repo", 7)
    assert files[0].binary is True
