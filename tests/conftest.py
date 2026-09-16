"""Shared fixtures.

Two properties every test in this suite depends on:

* **Environment isolation.** DevLens reads a lot from the environment, and a
  leaked variable turns a deterministic test into a flaky one — or worse, into
  one that silently enables a model. The autouse fixture strips every relevant
  variable before each test.
* **State isolation.** The application object is module-level, so its state is
  rebuilt per test rather than inherited from whichever test ran first.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import pytest
from starlette.datastructures import State

from devlens.app.api import app
from devlens.evidence import reset_ids

ENV_PREFIXES = ("DEVLENS_", "ANTHROPIC_", "OPENAI_", "CODEX_", "CLAUDE_")
ENV_EXACT = ("GH_TOKEN", "GITHUB_TOKEN", "CI")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    """No DevLens or vendor variable survives from the host or another test."""
    for key in list(os.environ):
        if key.startswith(ENV_PREFIXES) or key in ENV_EXACT:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEVLENS_JOBS_DB", str(tmp_path / "jobs.sqlite"))
    monkeypatch.setenv("DEVLENS_ANALYZE_ROOT", str(tmp_path))
    monkeypatch.setenv("DEVLENS_SANDBOX_RUNTIME", "none")
    # CI and this development container run as root. The refusal that protects a
    # real host is exercised directly in test_sandbox_isolation.py; every other
    # test opts in, the way a disposable CI container would.
    monkeypatch.setenv("DEVLENS_ALLOW_ROOT_REPO_TESTS", "1")
    reset_ids()
    yield


@pytest.fixture(autouse=True)
def isolated_application_state():
    app.state = State()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def jobs_db(tmp_path) -> Path:
    return tmp_path / "jobs.sqlite"


@pytest.fixture
def single_project(monkeypatch):
    """A minimal valid single-project configuration."""
    monkeypatch.setenv("DEVLENS_JIRA_URL", "https://team.atlassian.net")
    monkeypatch.setenv("DEVLENS_JIRA_EMAIL", "operator@example.com")
    monkeypatch.setenv("DEVLENS_JIRA_TOKEN", "jira-token")
    monkeypatch.setenv("DEVLENS_GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("DEVLENS_REPOSITORIES", "org/repo")
    monkeypatch.setenv("DEVLENS_JIRA_PROJECTS", "DEV")


# --------------------------------------------------------------------------- #
# Provider fixtures
# --------------------------------------------------------------------------- #

SHA = "a" * 40
HEAD = "b" * 40


DEFAULT_DIFF = (
    b"diff --git a/src/checkout.py b/src/checkout.py\n"
    b"--- a/src/checkout.py\n+++ b/src/checkout.py\n"
    b"@@ -1,2 +1,3 @@\n context\n+added line\n"
)


def github_transport(
    *,
    files: list[dict] | None = None,
    pr_files: list[dict] | None = None,
    pr_pages: int = 1,
    content: bytes = b"def checkout():\n    # instagram reply timeout\n    pass\n",
    diff: bytes = DEFAULT_DIFF,
) -> httpx.MockTransport:
    tree = files or [
        {"path": "src/checkout.py", "type": "blob", "mode": "100644", "size": 120}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        page = int(request.url.params.get("page", 1))
        if path.endswith("/files"):
            body = pr_files or [{"filename": "src/checkout.py", "status": "modified", "patch": "@@"}]
            headers = (
                {
                    "Link": f'<https://api.github.com{path}?page={page + 1}>; rel="next"'
                }
                if page < pr_pages
                else {}
            )
            return httpx.Response(200, json=body, headers=headers)
        if path.endswith("/commits") and "/pulls/" in path:
            return httpx.Response(
                200,
                json=[{"sha": SHA, "commit": {"message": "m", "author": {"name": "n"}}}],
            )
        if "/commits/" in path:
            return httpx.Response(200, json={"sha": SHA})
        if "/git/trees/" in path:
            return httpx.Response(200, json={"truncated": False, "tree": tree})
        if "/contents/" in path:
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "size": len(content),
                    "content": base64.b64encode(content).decode(),
                },
            )
        if "/pulls/" in path:
            if request.headers.get("Accept") == "application/vnd.github.diff":
                return httpx.Response(200, content=diff)
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "title": "Fix checkout",
                    "body": "body",
                    "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": "org/repo"}},
                    "base": {"ref": "main", "sha": SHA, "repo": {"full_name": "org/repo"}},
                    "state": "open",
                    "user": {"login": "dev"},
                    "html_url": "https://github.com/org/repo/pull/7",
                },
            )
        if path.rstrip("/").endswith("/repos/org/repo"):
            return httpx.Response(200, json={"default_branch": "trunk"})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def jira_transport(
    *, comments: int = 2, total: int | None = None, attachments: list[dict] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/comment"):
            start = int(request.url.params.get("startAt", 0))
            size = int(request.url.params.get("maxResults", 100))
            declared = total if total is not None else comments
            batch = [
                {
                    "id": str(index),
                    "author": {"displayName": "Reporter"},
                    "created": "2026-01-01T00:00:00Z",
                    "body": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": f"comment {index}"}],
                            }
                        ],
                    },
                }
                for index in range(start, min(start + size, comments))
            ]
            return httpx.Response(
                200, json={"comments": batch, "total": declared, "startAt": start}
            )
        if "/attachment/content/" in path:
            return httpx.Response(200, content=b"log line one\nlog line two\n")
        return httpx.Response(
            200,
            json={
                "fields": {
                    "summary": "Checkout timeout on instagram reply",
                    "description": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Users report a checkout timeout when "
                                        "posting an instagram reply.\n"
                                        "Expected: the reply posts.\n"
                                        "Observed: it times out.\n"
                                        "Acceptance criteria:\n"
                                        "- the reply posts within five seconds",
                                    }
                                ],
                            }
                        ],
                    },
                    "status": {"name": "Open"},
                    "priority": {"name": "High"},
                    "reporter": {"displayName": "Reporter"},
                    "labels": ["checkout"],
                    "components": [{"name": "payments"}],
                    "attachment": attachments or [],
                }
            },
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def git_repo(tmp_path):
    """A real git repository, for workspace and analyze tests."""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(
        "def checkout():\n    # instagram reply timeout\n    return True\n"
    )
    (root / "README.md").write_text("# Demo\nA checkout service.\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=Tester",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    return root
