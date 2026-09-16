"""Contract tests against a live provider.

Every other provider test in this suite runs against a transport fixture, which
proves DevLens parses the shape it *expects*. These run against the real GitHub
REST API — unauthenticated, one small public repository — and prove it parses
the shape GitHub actually returns.

They are skipped, loudly and with a reason, when the network or the API is not
available. A skip here is a gap in verification and says so; it is never a pass.
"""

from __future__ import annotations

import os

import httpx
import pytest

from devlens.domain import ProviderError
from devlens.providers.factory import GitProviderFactory, git_strategy

LIVE_REPOSITORY = "octocat/Hello-World"
LIVE_PULL_REQUEST = 1
SKIP_VARIABLE = "DEVLENS_SKIP_LIVE_CONTRACT"

pytestmark = pytest.mark.live


def reachable() -> tuple[bool, str]:
    if os.environ.get(SKIP_VARIABLE, "").strip():
        return False, f"{SKIP_VARIABLE} is set"
    try:
        response = httpx.get("https://api.github.com/rate_limit", timeout=10.0)
    except httpx.RequestError as exc:
        return False, f"api.github.com is unreachable ({type(exc).__name__})"
    if response.status_code == 403:
        return False, "the unauthenticated GitHub rate limit is exhausted"
    if response.status_code != 200:
        return False, f"api.github.com answered {response.status_code}"
    remaining = int(response.json()["resources"]["core"]["remaining"])
    if remaining < 10:
        return False, f"only {remaining} unauthenticated GitHub calls remain"
    return True, ""


AVAILABLE, REASON = reachable()
requires_github = pytest.mark.skipif(not AVAILABLE, reason=REASON or "GitHub is unavailable")


@pytest.fixture
async def github():
    """The real adapter, built the way the application builds it."""
    strategy = git_strategy("github")
    client = strategy.client(token="", email="")
    # Unauthenticated: the public endpoints this test uses need no credential,
    # and sending an empty bearer would be rejected.
    client.headers.pop("Authorization", None)
    async with client:
        yield GitProviderFactory.create("github", client)


@requires_github
@pytest.mark.asyncio
async def test_the_default_branch_comes_back_as_a_branch_name(github):
    branch = await github.default_branch(LIVE_REPOSITORY)
    assert isinstance(branch, str) and branch
    assert "/" not in branch


@requires_github
@pytest.mark.asyncio
async def test_a_ref_resolves_to_a_forty_character_sha(github):
    commit = await github.resolve_ref(LIVE_REPOSITORY, "HEAD")
    assert len(commit) == 40
    assert set(commit) <= set("0123456789abcdef")


@requires_github
@pytest.mark.asyncio
async def test_the_tree_parses_into_repository_files(github):
    commit = await github.resolve_ref(LIVE_REPOSITORY, "HEAD")
    files = await github.list_files(LIVE_REPOSITORY, commit)
    assert files, "a public repository should list at least one blob"
    for item in files:
        assert item.path and not item.path.startswith("/")
        assert item.size >= 0


@requires_github
@pytest.mark.asyncio
async def test_a_real_file_decodes_and_its_permalink_pins_the_commit(github):
    commit = await github.resolve_ref(LIVE_REPOSITORY, "HEAD")
    files = await github.list_files(LIVE_REPOSITORY, commit)
    target = min(files, key=lambda item: item.size)
    content = await github.read_file(LIVE_REPOSITORY, commit, target.path)
    assert isinstance(content, str)

    url = github.evidence_url(LIVE_REPOSITORY, commit, target.path, 1)
    assert commit in url, "a permalink must pin the commit, never a branch"
    assert url.endswith("#L1")
    # The link resolves: the evidence an operator clicks is real.
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        response = await client.head(url)
    assert response.status_code == 200


@requires_github
@pytest.mark.asyncio
async def test_a_real_pull_request_parses_with_its_files_and_commits(github):
    pull = await github.get_pull_request(LIVE_REPOSITORY, LIVE_PULL_REQUEST)
    assert pull.number == LIVE_PULL_REQUEST
    assert len(pull.head_sha) == 40
    assert pull.target_branch
    assert pull.url.startswith("https://github.com/")

    files, more = await github.get_pull_request_files(LIVE_REPOSITORY, LIVE_PULL_REQUEST)
    assert isinstance(more, bool)
    for item in files:
        assert item.path
        assert item.status in {"added", "modified", "removed", "renamed", "copied", "changed", "unchanged"}

    commits, truncated = await github.list_commits(LIVE_REPOSITORY, LIVE_PULL_REQUEST)
    assert isinstance(truncated, bool)
    for commit in commits:
        assert len(commit.sha) == 40


@requires_github
@pytest.mark.asyncio
async def test_a_real_diff_is_returned_as_text(github):
    diff = await github.get_diff(LIVE_REPOSITORY, LIVE_PULL_REQUEST)
    assert isinstance(diff, str)
    if diff.strip():
        assert diff.lstrip().startswith("diff --git")


@requires_github
@pytest.mark.asyncio
async def test_a_repository_that_does_not_exist_is_a_provider_error(github):
    """The 404 path, against the real API rather than a fixture."""
    with pytest.raises(ProviderError):
        await github.default_branch("devlens-does-not-exist/nor-does-this")


@requires_github
@pytest.mark.asyncio
async def test_the_live_shape_still_matches_the_fixture_the_suite_uses(github):
    """If GitHub changes its payload, this is the test that notices.

    The rest of the suite runs against ``tests/conftest.github_transport``. That
    is only meaningful while the fixture still resembles the real thing, so the
    field names the adapter depends on are checked against a live response here.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"https://api.github.com/repos/{LIVE_REPOSITORY}/pulls/{LIVE_PULL_REQUEST}"
        )
    assert response.status_code == 200
    body = response.json()
    for field in ("number", "title", "body", "state", "html_url", "head", "base", "user"):
        assert field in body, f"GitHub no longer returns {field!r}; the fixture is stale"
    assert "sha" in body["head"] and "ref" in body["head"]
    assert "sha" in body["base"] and "ref" in body["base"]
