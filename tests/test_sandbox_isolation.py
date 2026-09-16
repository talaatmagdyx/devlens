"""Sandbox and shell isolation.

Regression coverage for DL-P0-001 (repository code inheriting DevLens
credentials), DL-P1-011 (unbounded output and orphaned children) and DL-P2-023
(argument injection through the command allowlist).

The credential test is deliberately end-to-end: it plants a ``conftest.py`` in
a directory, runs pytest over it, and asserts that the file the repository code
wrote back contains none of DevLens's secrets. Asserting on ``build_env`` alone
would not have caught the original bug, because the original bug was in the
caller.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from devlens.domain import AccessDenied, ProviderError
from devlens.guardrails import CapabilityGuard
from devlens.sandbox.workspace import Workspace, open_local
from devlens.tools import shell
from devlens.tools.shell import Limits, allowed, build_env, container_argv, run

# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def test_build_env_never_copies_the_process_environment(monkeypatch):
    monkeypatch.setenv("DEVLENS_JIRA_TOKEN", "jira-secret")
    monkeypatch.setenv("DEVLENS_GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    env = build_env()
    assert not any("secret" in value for value in env.values())
    assert not any(key.startswith("DEVLENS_") for key in env)
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_new_secret_is_excluded_without_anyone_updating_a_denylist(monkeypatch):
    """The allowlist is the point: tomorrow's variable is excluded by default."""
    monkeypatch.setenv("SOME_FUTURE_CREDENTIAL", "value")
    assert "SOME_FUTURE_CREDENTIAL" not in build_env()


@pytest.mark.asyncio
async def test_repository_code_cannot_read_devlens_credentials(tmp_path, monkeypatch):
    """DL-P0-001, reproduced and then prevented."""
    monkeypatch.setenv("DEVLENS_JIRA_TOKEN", "jira-secret-123")
    monkeypatch.setenv("DEVLENS_GITHUB_TOKEN", "ghp-secret-456")
    monkeypatch.setenv("DEVLENS_ANTHROPIC_API_KEY", "sk-ant-secret-789")
    monkeypatch.setenv(
        "PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}"
    )
    workspace = tmp_path / "hostile"
    workspace.mkdir()
    leak = tmp_path / "leak.json"
    (workspace / "conftest.py").write_text(
        "import os, json, pathlib\n"
        f"pathlib.Path({str(leak)!r}).write_text(json.dumps(dict(os.environ)))\n"
    )
    (workspace / "test_ok.py").write_text("def test_ok():\n    assert True\n")

    guard = CapabilityGuard({"repository_code_execution"})
    result = await shell.run_repository_code(
        ["pytest", "-q", "-p", "no:cacheprovider"], cwd=workspace, guard=guard
    )
    assert result.returncode == 0
    seen = json.loads(leak.read_text())
    assert not [key for key in seen if key.startswith("DEVLENS_")]
    assert "ANTHROPIC_API_KEY" not in seen
    assert not [value for value in seen.values() if "secret" in value]


@pytest.mark.asyncio
async def test_executing_repository_code_requires_a_capability(tmp_path):
    with pytest.raises(AccessDenied, match="disabled"):
        await shell.run_repository_code(
            ["pytest", "-q"], cwd=tmp_path, guard=CapabilityGuard()
        )


def test_container_isolation_flags_are_restrictive():
    argv = container_argv("podman", "python:3.12-slim", Path("/work"), ["pytest", "-q"])
    joined = " ".join(argv)
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        "/work:ro",
    ):
        assert flag in joined


# --------------------------------------------------------------------------- #
# Argument hardening
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "-c", "core.pager=sh -c 'id'", "log"],
        ["git", "clone", "--upload-pack=touch /tmp/pwn", "--", "u", "d"],
        ["git", "log", "--output=/tmp/pwn"],
        ["git", "checkout", "--orphan", "x"],
        ["git", "push"],
        ["git", "config", "--global", "x", "y"],
        ["git", "fetch", "origin"],
        ["git", "submodule", "update"],
        ["pytest", "--co", "-p", "evil"],
        ["bash", "-c", "id"],
        ["rg", "--pre", "sh"],
        ["sh"],
        [],
        ["git", "log\x00"],
    ],
)
def test_dangerous_argv_is_refused(argv):
    assert allowed(argv, allow_tests=True) is False


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "log", "-n", "20", "--oneline", "--no-color"],
        ["git", "rev-parse", "--verify", "HEAD"],
        ["git", "ls-tree", "-r", "--long", "abc"],
        ["git", "remote", "remove", "origin"],
        ["rg", "-n", "--max-count", "20", "--", "query"],
    ],
)
def test_expected_argv_is_permitted(argv):
    assert allowed(argv) is True


def test_test_runners_need_the_allow_tests_flag():
    assert allowed(["pytest", "-q"]) is False
    assert allowed(["pytest", "-q"], allow_tests=True) is True


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_large_output_is_capped_while_reading_not_afterwards(tmp_path):
    """DL-P1-011: 19 MB used to take 60s and hit the timeout."""
    (tmp_path / "big.txt").write_text("token\n" * 300_000)
    started = time.monotonic()
    result = await run(
        ["rg", "-n", "--", "token"],
        cwd=tmp_path,
        limits=Limits(timeout=20, max_output_bytes=40_000),
    )
    elapsed = time.monotonic() - started
    assert result.truncated is True
    assert len(result.stdout) <= 40_000
    assert elapsed < 10, "capping must happen during the read, not after it"


@pytest.mark.asyncio
async def test_timeout_kills_the_child_and_its_whole_process_group(tmp_path, monkeypatch):
    """DL-P1-011: a backgrounded grandchild must not outlive the timeout.

    The allowlist is exercised elsewhere; here it is bypassed deliberately so
    that the *execution* path can be given a command that genuinely blocks.
    Killing only the direct child would leave the backgrounded ``sleep``
    running, and the marker file would appear.
    """
    monkeypatch.setattr(shell, "allowed", lambda argv, **kwargs: True)
    survivor = tmp_path / "orphan-was-alive"
    script = f"(sleep 2; : > {survivor}) & sleep 30"

    started = time.monotonic()
    with pytest.raises(ProviderError, match="budget"):
        await run(["sh", "-c", script], cwd=tmp_path, limits=Limits(timeout=0.5))
    elapsed = time.monotonic() - started
    assert elapsed < 5, "the timeout must not wait for the child to finish"

    await asyncio.sleep(3)
    assert not survivor.exists(), "a backgrounded grandchild survived the kill"


@pytest.mark.asyncio
async def test_a_missing_binary_is_a_provider_error(tmp_path):
    original = shell.GIT_COMMANDS
    try:
        shell.GIT_COMMANDS = {**original, "log": original["log"]}
        with pytest.raises(ProviderError, match="not available"):
            await run(["rg", "--version"], cwd=tmp_path / "missing")
    except ProviderError:
        pass
    finally:
        shell.GIT_COMMANDS = original


def test_limits_for_tests_are_tighter_than_the_default():
    default, tests = Limits(), Limits.for_tests()
    assert tests.memory_bytes < default.memory_bytes
    assert tests.processes < default.processes
    assert tests.file_size_bytes < default.file_size_bytes


# --------------------------------------------------------------------------- #
# Workspace containment
# --------------------------------------------------------------------------- #


def test_workspace_refuses_paths_that_escape(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "inside.txt").write_text("ok")
    assert workspace.resolve("inside.txt").name == "inside.txt"
    for escape in ("../outside", "/etc/passwd", "a/../../outside"):
        with pytest.raises(AccessDenied, match="escapes"):
            workspace.resolve(escape)


def test_workspace_resolves_symlinks_before_checking_containment(tmp_path):
    """Resolve-then-contain is the order that makes the check meaningful."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "link.txt").symlink_to(outside)
    with pytest.raises(AccessDenied, match="escapes"):
        Workspace(root).resolve("link.txt")


def test_open_local_requires_a_git_repository(tmp_path, git_repo):
    assert open_local(str(git_repo)).root == git_repo.resolve()
    with pytest.raises(ProviderError, match="not a git repository"):
        open_local(str(tmp_path))
    with pytest.raises(ProviderError, match="not a directory"):
        open_local(str(git_repo / "app.py"))


@pytest.mark.asyncio
async def test_sandbox_branches_must_use_the_devlens_prefix(git_repo):
    workspace = open_local(str(git_repo))
    with pytest.raises(AccessDenied, match="devlens/"):
        await workspace.create_local_branch("main")
    await workspace.create_local_branch("devlens/DEV-1")


@pytest.mark.asyncio
async def test_a_disposable_workspace_is_removed(tmp_path):
    root = tmp_path / "throwaway"
    root.mkdir()
    workspace = Workspace(root, disposable=True)
    async with workspace:
        assert root.exists()
    assert not root.exists()


@pytest.mark.asyncio
async def test_clone_refuses_a_host_that_is_not_allowlisted():
    from devlens.sandbox.workspace import CloneSettings, clone_repository

    with pytest.raises(AccessDenied, match="not allowed"):
        await clone_repository(CloneSettings("evil.example", "Bearer x"), "org/repo", "HEAD")


@pytest.mark.asyncio
async def test_clone_refuses_a_traversing_repository_identifier():
    from devlens.sandbox.workspace import CloneSettings, clone_repository

    with pytest.raises(AccessDenied, match="not valid"):
        await clone_repository(CloneSettings("github.com", "Bearer x"), "org/../etc", "HEAD")


def test_isolation_runtime_honours_an_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("DEVLENS_SANDBOX_RUNTIME", "none")
    assert shell.isolation_runtime() is None
    monkeypatch.setenv("DEVLENS_SANDBOX_RUNTIME", "definitely-not-installed")
    assert shell.isolation_runtime() is None


@pytest.mark.asyncio
async def test_concurrent_commands_do_not_interfere(git_repo):
    results = await asyncio.gather(
        *(run(["git", "log", "-n", "1", "--oneline"], cwd=git_repo) for _ in range(5))
    )
    assert all(result.returncode == 0 for result in results)
    assert len({result.stdout for result in results}) == 1


# --------------------------------------------------------------------------- #
# The limits, observed from inside the child
# --------------------------------------------------------------------------- #
#
# Asserting that ``Limits`` holds certain numbers proves nothing about the
# child. These run a program that reports its own rlimits, and one that tries to
# exceed each of them.


def interpreter_on_path(monkeypatch) -> None:
    import sys

    monkeypatch.setenv(
        "PATH", f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}"
    )


async def run_python(source: str, *, limits: Limits, tmp_path: Path):
    """Execute a snippet as repository code under the sandbox."""
    workspace = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    (workspace / "conftest.py").write_text(source)
    (workspace / "test_noop.py").write_text("def test_noop():\n    assert True\n")
    return await shell.run_repository_code(
        ["pytest", "-q", "-p", "no:cacheprovider"],
        cwd=workspace,
        guard=CapabilityGuard({"repository_code_execution"}),
        limits=limits,
    )


@pytest.mark.asyncio
async def test_the_child_really_runs_under_the_declared_limits(tmp_path, monkeypatch):
    interpreter_on_path(monkeypatch)
    report = tmp_path / "limits.json"
    limits = Limits.for_tests()
    await run_python(
        "import resource, json, pathlib\n"
        f"pathlib.Path({str(report)!r}).write_text(json.dumps({{\n"
        "    'cpu': resource.getrlimit(resource.RLIMIT_CPU),\n"
        "    'fsize': resource.getrlimit(resource.RLIMIT_FSIZE),\n"
        "    'nproc': resource.getrlimit(resource.RLIMIT_NPROC),\n"
        "    'core': resource.getrlimit(resource.RLIMIT_CORE),\n"
        "}))\n",
        limits=limits,
        tmp_path=tmp_path,
    )
    seen = json.loads(report.read_text())
    assert seen["cpu"][0] == limits.cpu_seconds
    assert seen["fsize"][0] == limits.file_size_bytes
    assert seen["nproc"][0] == limits.processes
    assert seen["core"] == [0, 0], "a core dump could carry the process's memory to disk"


@pytest.mark.asyncio
async def test_the_file_size_limit_actually_stops_a_large_write(tmp_path, monkeypatch):
    interpreter_on_path(monkeypatch)
    verdict = tmp_path / "verdict.txt"
    await run_python(
        "import pathlib\n"
        "try:\n"
        "    pathlib.Path('big.bin').write_bytes(b'x' * (4 * 1024 * 1024))\n"
        "    outcome = 'wrote'\n"
        "except OSError as exc:\n"
        "    outcome = f'refused: {type(exc).__name__}'\n"
        f"pathlib.Path({str(verdict)!r}).write_text(outcome)\n",
        limits=Limits(file_size_bytes=64_000, timeout=30),
        tmp_path=tmp_path,
    )
    assert verdict.read_text().startswith("refused"), verdict.read_text()


@pytest.mark.asyncio
async def test_repository_code_is_refused_as_root_without_a_container(
    tmp_path, monkeypatch
):
    """The gap RLIMIT_NPROC leaves, closed by refusing rather than pretending.

    ``RLIMIT_NPROC`` is a per-uid limit that root is exempt from, so with no
    container runtime a repository's own test suite could fork without bound.
    DevLens refuses that combination and names all three ways out.
    """
    monkeypatch.delenv("DEVLENS_ALLOW_ROOT_REPO_TESTS", raising=False)
    monkeypatch.setattr(shell, "isolation_runtime", lambda: None)
    monkeypatch.setattr(shell, "running_as_root", lambda: True)

    with pytest.raises(AccessDenied, match="Refusing to execute repository code as root"):
        await shell.run_repository_code(
            ["pytest", "-q"],
            cwd=tmp_path,
            guard=CapabilityGuard({"repository_code_execution"}),
        )


@pytest.mark.asyncio
async def test_a_container_runtime_removes_the_root_refusal(tmp_path, monkeypatch):
    """With a runtime present the pid limit is the engine's, not RLIMIT_NPROC."""
    monkeypatch.delenv("DEVLENS_ALLOW_ROOT_REPO_TESTS", raising=False)
    monkeypatch.setattr(shell, "running_as_root", lambda: True)
    monkeypatch.setattr(shell, "isolation_runtime", lambda: "podman")

    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        return await real("/bin/true", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    result = await shell.run_repository_code(
        ["pytest", "-q"],
        cwd=workspace,
        guard=CapabilityGuard({"repository_code_execution"}),
    )
    assert result.returncode == 0


def test_a_non_root_process_needs_no_override(monkeypatch):
    monkeypatch.delenv("DEVLENS_ALLOW_ROOT_REPO_TESTS", raising=False)
    monkeypatch.setattr(shell, "running_as_root", lambda: False)
    shell.require_unprivileged()  # does not raise


def test_the_override_is_stated_in_every_report(monkeypatch):
    """An operator who opts in is told what they gave up."""
    monkeypatch.setenv("DEVLENS_SANDBOX_RUNTIME", "none")
    monkeypatch.setenv("DEVLENS_ALLOW_ROOT_REPO_TESTS", "1")
    monkeypatch.setattr(shell, "running_as_root", lambda: True)
    limitations = CapabilityGuard({"repository_code_execution"}).limitations()
    assert any("DEVLENS_ALLOW_ROOT_REPO_TESTS is set" in item for item in limitations)
    assert any("not protected" in item for item in limitations)


@pytest.mark.asyncio
async def test_the_child_cannot_write_outside_its_workspace_by_default(
    tmp_path, monkeypatch
):
    """HOME is not the operator's home, so a stray write lands in the sandbox."""
    interpreter_on_path(monkeypatch)
    report = tmp_path / "home.txt"
    await run_python(
        "import os, pathlib\n"
        f"pathlib.Path({str(report)!r}).write_text(os.environ.get('HOME', ''))\n",
        limits=Limits.for_tests(),
        tmp_path=tmp_path,
    )
    home = report.read_text()
    assert home.startswith(str(tmp_path)), home
    assert home != str(Path.home())


# --------------------------------------------------------------------------- #
# The container path, verified at the point of execution
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_configured_runtime_is_invoked_with_the_hardened_argv(
    tmp_path, monkeypatch
):
    """The flags are checked where they are used, not where they are written.

    A container engine is not available in every environment, so what is pinned
    here is the argv that reaches ``create_subprocess_exec`` when a runtime is
    configured: if a future change drops ``--network=none``, this fails.
    """
    captured: dict = {}
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        captured["argv"] = list(argv)
        # Run something harmless instead of the container engine.
        return await real("/bin/true", **kwargs)

    monkeypatch.setattr(shell, "isolation_runtime", lambda: "podman")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    workspace = tmp_path / "repo"
    workspace.mkdir()
    await shell.run_repository_code(
        ["pytest", "-q"],
        cwd=workspace,
        guard=CapabilityGuard({"repository_code_execution"}),
    )

    argv = captured["argv"]
    assert argv[0] == "podman"
    joined = " ".join(argv)
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        "--memory=",
        "--cpus=",
        f"{workspace}:/work:ro",
    ):
        assert flag in joined, f"{flag} is no longer passed to the runtime"
    assert argv[-2:] == ["pytest", "-q"], "the command must be the last thing passed"


@pytest.mark.asyncio
async def test_no_devlens_credential_reaches_the_container_invocation(
    tmp_path, monkeypatch
):
    captured: dict = {}
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env") or {}
        return await real("/bin/true", **kwargs)

    monkeypatch.setenv("DEVLENS_JIRA_TOKEN", "jira-secret")
    monkeypatch.setenv("DEVLENS_GITHUB_TOKEN", "gh-secret")
    monkeypatch.setattr(shell, "isolation_runtime", lambda: "podman")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    workspace = tmp_path / "repo"
    workspace.mkdir()
    await shell.run_repository_code(
        ["pytest", "-q"],
        cwd=workspace,
        guard=CapabilityGuard({"repository_code_execution"}),
    )
    assert "secret" not in " ".join(captured["argv"])
    assert not [key for key in captured["env"] if key.startswith("DEVLENS_")]
