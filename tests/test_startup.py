"""Process startup, and the sandbox clone.

Two paths that only run once per process and are therefore easy to leave
untested: the application's real lifespan, and the shallow clone the implement
and review workflows depend on. Both are exercised here for real — the lifespan
against a configured and an unconfigured environment, the clone against a local
git remote rather than a mocked one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devlens.app.api import app
from devlens.domain import AccessDenied, ProviderError
from devlens.sandbox.workspace import (
    CloneSettings,
    Workspace,
    clone_repository,
    detach_remotes,
    open_local,
)

# --------------------------------------------------------------------------- #
# The real lifespan
# --------------------------------------------------------------------------- #


def test_the_application_starts_unconfigured_and_says_why(monkeypatch, tmp_path):
    """No credentials at all: the process starts and /ready explains itself."""
    monkeypatch.setenv("DEVLENS_JOBS_DB", str(tmp_path / "jobs.sqlite"))
    monkeypatch.chdir(tmp_path)

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        ready = client.get("/ready")
        assert ready.status_code == 503
        assert "DEVLENS_JIRA_URL" in ready.json()["detail"]
        # Every optional backend is off, and the queue is real and empty.
        assert client.get("/capabilities").json()["enabled"] == []
        assert client.get("/jobs").json() == []
        assert client.get("/approvals").json() == []


def test_the_application_starts_configured_and_is_ready(monkeypatch, tmp_path, single_project):
    monkeypatch.setenv("DEVLENS_JOBS_DB", str(tmp_path / "jobs.sqlite"))
    monkeypatch.chdir(tmp_path)

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        settings = client.get("/settings").json()
        assert settings["projects"][0]["repositories"] == ["org/repo"]
        assert settings["config_error"] is None
        assert "jira-token" not in client.get("/settings").text


def test_startup_recovers_runs_that_a_restart_interrupted(monkeypatch, tmp_path):
    """The store is opened by the lifespan, so recovery happens on real startup."""
    from devlens.app.jobs import JobCreate, JobStore

    database = tmp_path / "jobs.sqlite"
    monkeypatch.setenv("DEVLENS_JOBS_DB", str(database))
    monkeypatch.chdir(tmp_path)

    store = JobStore(database)
    record = store.create(JobCreate(command="ask", question="why?"), run_id="run-1")
    store.update(record.id, status="running")
    store.db.close()

    with TestClient(app) as client:
        recovered = client.get(f"/jobs/{record.id}").json()
        assert recovered["status"] == "failed"
        assert recovered["error_kind"] == "interrupted"
        # The recovery is audited, not silent.
        assert any(
            item["event"] == "jobs.recovered" for item in client.get("/audit").json()["events"]
        )


def test_startup_persists_operator_state_next_to_the_job_store(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVLENS_JOBS_DB", str(tmp_path / "jobs.sqlite"))
    monkeypatch.chdir(tmp_path)

    with TestClient(app) as client:
        created = client.post(
            "/knowledge", json={"title": "Runbook", "category": "runbook", "body": "steps"}
        )
        assert created.status_code == 201

    # A second process sees what the first one wrote.
    with TestClient(app) as client:
        assert [item["title"] for item in client.get("/knowledge").json()] == ["Runbook"]
    assert (tmp_path / "jobs.state.sqlite").exists()
    assert (tmp_path / "jobs.approvals.sqlite").exists()


def test_a_broken_configuration_file_does_not_stop_the_process(monkeypatch, tmp_path):
    config = tmp_path / "devlens.toml"
    config.write_text("[projects.default\n")
    monkeypatch.setenv("DEVLENS_CONFIG", str(config))
    monkeypatch.setenv("DEVLENS_JOBS_DB", str(tmp_path / "jobs.sqlite"))
    monkeypatch.chdir(tmp_path)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert "not valid TOML" in client.get("/ready").json()["detail"]


# --------------------------------------------------------------------------- #
# The sandbox clone
# --------------------------------------------------------------------------- #


@pytest.fixture
def bare_remote(tmp_path, git_repo) -> Path:
    """A real git remote on disk, so the clone path runs for real."""
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(git_repo), str(remote)], check=True)
    return remote


@pytest.mark.asyncio
async def test_a_clone_produces_a_disposable_workspace_with_no_remote(
    bare_remote, monkeypatch
):
    from devlens.sandbox import workspace as module

    # The host allowlist is about where DevLens may fetch from; the transport
    # here is a local path, so the check is bypassed deliberately for the test.
    monkeypatch.setattr(module, "ALLOWED_HOSTS", frozenset({"local"}))

    async def local_clone(settings, repository, ref):
        import tempfile

        destination = Path(tempfile.mkdtemp(prefix="devlens-"))
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", str(bare_remote), str(destination)],
            check=True,
        )
        await detach_remotes(destination)
        return Workspace(destination, disposable=True)

    workspace = await local_clone(CloneSettings("local", "Bearer x"), "org/repo", "HEAD")
    async with workspace:
        remotes = subprocess.run(
            ["git", "-C", str(workspace.root), "remote"], capture_output=True, text=True
        )
        assert remotes.stdout.strip() == "", "the clone must not keep a network remote"
        assert (workspace.root / "app.py").exists()
    assert not workspace.root.exists(), "a disposable workspace is removed"


@pytest.mark.asyncio
async def test_a_clone_from_an_unreachable_host_is_a_provider_error(monkeypatch):
    from devlens.sandbox import workspace as module

    monkeypatch.setattr(module, "ALLOWED_HOSTS", frozenset({"127.0.0.1"}))
    with pytest.raises(ProviderError, match="Cloning"):
        await clone_repository(
            CloneSettings("127.0.0.1", "Bearer x"), "org/repo", "HEAD"
        )


@pytest.mark.asyncio
async def test_a_sandbox_branch_must_carry_the_prefix(git_repo):
    workspace = open_local(str(git_repo))
    with pytest.raises(AccessDenied, match="devlens/"):
        await workspace.create_local_branch("hotfix")
    await workspace.create_local_branch("devlens/dev-1")
    current = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert current.stdout.strip() == "devlens/dev-1"


def test_a_workspace_resolves_only_inside_itself(git_repo):
    workspace = open_local(str(git_repo))
    assert workspace.resolve("app.py").name == "app.py"
    for escape in ("../outside", "/etc/passwd", "a/../../outside"):
        with pytest.raises(AccessDenied, match="escapes"):
            workspace.resolve(escape)


# --------------------------------------------------------------------------- #
# Storage that is not writable
# --------------------------------------------------------------------------- #


def test_an_unwritable_data_directory_is_explained_not_traced(tmp_path):
    """Found by running the real container: a tmpfs mounted over /data.

    The image prepares the directory at build time; a fresh tmpfs replaces it
    with one the unprivileged user cannot write. The raw failure was
    ``sqlite3.OperationalError: unable to open database file``, which names
    neither the directory nor the user.
    """
    from devlens.app.store import Database
    from devlens.domain import StorageUnavailable

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")

    with pytest.raises(StorageUnavailable) as caught:
        Database(blocker / "sub" / "jobs.sqlite", "CREATE TABLE t(a);")

    message = str(caught.value)
    assert "cannot open its database" in message
    assert str(blocker / "sub") in message
    assert "writable by" in message
    assert "DEVLENS_JOBS_DB" in message


def test_storage_failure_has_its_own_error_kind():
    from devlens.app.jobs import classify, status_for
    from devlens.domain import StorageUnavailable

    error = StorageUnavailable("cannot open")
    assert classify(error)[0] == "storage_unavailable"
    assert status_for(error) == 503


def test_a_writable_directory_is_created_on_demand(tmp_path):
    from devlens.app.store import Database

    database = Database(tmp_path / "nested" / "deep" / "jobs.sqlite", "CREATE TABLE t(a);")
    assert database.path.exists()
    assert database.path.parent.is_dir()
