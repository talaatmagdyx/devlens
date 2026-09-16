"""Bounded subprocess execution.

Two rules govern everything in this module.

**Nothing is inherited.** A child process receives an environment built from an
allowlist, never a copy of DevLens's own. Repository code executed by
``review --run-tests`` or ``implement --run-tests`` therefore cannot read
``DEVLENS_JIRA_TOKEN``, ``DEVLENS_GITHUB_TOKEN`` or any model credential, even
though it runs on the same machine.

**Nothing is unbounded.** Output is streamed and capped as it arrives rather
than buffered and truncated afterwards, wall-clock and CPU time are limited,
and on timeout or overflow the whole process *group* is killed rather than
abandoned.

Argument construction is equally strict: each command has an explicit option
allowlist, ``git -c`` is refused outright, and every user-supplied value is
placed after a ``--`` separator so it can never be read as an option.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from dataclasses import dataclass, field, replace
from pathlib import Path

from devlens.domain import AccessDenied, ProviderError
from devlens.guardrails import enabled_flag

# --------------------------------------------------------------------------- #
# Command allowlist
# --------------------------------------------------------------------------- #

#: Per-verb option allowlist. Any argv element beginning with "-" must appear
#: here exactly, or as the left-hand side of an ``--option=value`` pair.
GIT_COMMANDS: dict[str, frozenset[str]] = {
    "log": frozenset({"-n", "--oneline", "--no-color", "--max-count", "--"}),
    "show": frozenset({"--stat", "--no-color", "--"}),
    "blame": frozenset({"--line-porcelain", "--no-color", "--"}),
    "diff": frozenset({"--stat", "--no-color", "--name-only", "--"}),
    "status": frozenset({"--short", "--porcelain", "--"}),
    "rev-parse": frozenset({"--verify", "--"}),
    "ls-tree": frozenset({"-r", "--long", "--full-tree", "--"}),
    "ls-files": frozenset({"--"}),
    "grep": frozenset({"-n", "-I", "--fixed-strings", "--"}),
    "checkout": frozenset({"--detach", "-B", "--"}),
    "clone": frozenset({"--depth", "--no-tags", "--single-branch", "--"}),
}

#: Verbs that mutate a remote or rewrite history. Refused unconditionally, even
#: if a future option allowlist were added for them by mistake.
BLOCKED_GIT = frozenset(
    {
        "push",
        "commit",
        "config",
        "reset",
        "rebase",
        "filter-branch",
        "am",
        "apply",
        "fetch",
        "pull",
        "submodule",
        "lfs",
        "daemon",
        "send-email",
        "request-pull",
    }
)

#: Fixed internal invocations, permitted verbatim and in no other form.
EXACT_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("git", "remote", "remove", "origin"),
        ("git", "--version"),
        ("rg", "--version"),
    }
)

SEARCH_OPTIONS = frozenset(
    {
        "-n",
        "-I",
        "--with-filename",
        "--max-count",
        "--max-filesize",
        "--glob",
        "--no-messages",
        "--color",
        "--hidden",
        "--",
    }
)

#: Commands that execute code from the repository under inspection. These are
#: only reachable through :func:`run_repository_code`, which requires the
#: ``repository_code_execution`` capability.
TEST_COMMANDS: dict[tuple[str, ...], frozenset[str]] = {
    ("pytest",): frozenset(
        {"-q", "-x", "-p", "--no-header", "--timeout", "--color", "--"}
    ),
    ("bundle", "exec", "rspec"): frozenset({"--no-color", "--format", "--"}),
}

# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #

#: The only variables a child may inherit. Everything else — every credential,
#: every DEVLENS_* setting — is dropped.
INHERITABLE = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SSL_CERT_FILE")

#: Variables always forced, regardless of what the caller asks for.
FORCED = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GCM_INTERACTIVE": "never",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PIP_NO_INPUT": "1",
    "NO_COLOR": "1",
}


def build_env(
    extra: dict[str, str] | None = None, *, home: Path | None = None
) -> dict[str, str]:
    """Build a child environment from the allowlist.

    This function is the fix for credential inheritance. It never reads a
    variable that is not in :data:`INHERITABLE`, so adding a new secret to
    DevLens's own environment cannot silently expose it to a subprocess.
    """
    env = {key: os.environ[key] for key in INHERITABLE if key in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(home) if home is not None else "/nonexistent"
    env.update(FORCED)
    if extra:
        env.update(extra)
    return env


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Limits:
    """Resource ceilings applied to a child process."""

    timeout: float = 60.0
    max_output_bytes: int = 256_000
    cpu_seconds: int = 120
    memory_bytes: int = 2 * 1024**3
    file_size_bytes: int = 256 * 1024**2
    processes: int = 512

    @classmethod
    def for_tests(cls) -> Limits:
        """Tighter ceilings for running a repository's own test suite."""
        return cls(
            timeout=180.0,
            max_output_bytes=128_000,
            cpu_seconds=150,
            memory_bytes=1536 * 1024**2,
            file_size_bytes=64 * 1024**2,
            processes=192,
        )


def _preexec(limits: Limits):  # pragma: no cover - runs in the forked child
    import resource

    def apply() -> None:
        # The process group is created by start_new_session=True; calling
        # setsid() again here would fail because we are already the leader.
        for what, value in (
            (resource.RLIMIT_CPU, limits.cpu_seconds),
            (resource.RLIMIT_FSIZE, limits.file_size_bytes),
            (resource.RLIMIT_NPROC, limits.processes),
        ):
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(what, (value, value))
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(
                resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes)
            )

    return apply


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _options_ok(argv: list[str], allowed: frozenset[str]) -> bool:
    for part in argv:
        if not part.startswith("-"):
            continue
        name = part.split("=", 1)[0]
        if name not in allowed:
            return False
    return True


def allowed(argv: list[str], *, allow_tests: bool = False) -> bool:
    """Whether this exact argv may be executed.

    Stricter than a verb allowlist: the option list is checked too, so
    ``git -c core.pager=... log`` and ``git clone --upload-pack=...`` are
    refused even though ``log`` and ``clone`` are permitted verbs.
    """
    if not argv or any("\x00" in part or "\n" in part for part in argv):
        return False
    if tuple(argv) in EXACT_COMMANDS:
        return True
    if argv[0] == "git":
        rest = argv[1:]
        if not rest or rest[0] in BLOCKED_GIT:
            return False
        # "git -c <key>=<value>" injects configuration into the child. Refused.
        if rest[0].startswith("-"):
            return False
        options = GIT_COMMANDS.get(rest[0])
        return options is not None and _options_ok(rest[1:], options)
    if argv[0] == "rg":
        return _options_ok(argv[1:], SEARCH_OPTIONS)
    for prefix, options in TEST_COMMANDS.items():
        if tuple(argv[: len(prefix)]) == prefix:
            return allow_tests and _options_ok(argv[len(prefix) :], options)
    return False


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False
    duration_ms: int = 0
    limits: Limits = field(default_factory=Limits)


async def _drain(
    stream: asyncio.StreamReader | None,
    cap: int,
    on_overflow=None,
) -> tuple[bytes, bool]:
    """Read a stream incrementally, stopping at ``cap`` bytes.

    Capping during the read is what keeps a command that emits tens of
    megabytes from spending the whole wall-clock budget moving bytes through a
    pipe. ``on_overflow`` is called the moment the ceiling is hit so the child
    can be killed rather than left blocking on a full pipe.
    """
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    size = 0
    while True:
        try:
            chunk = await stream.read(65_536)
        except (ValueError, asyncio.LimitOverrunError):  # pragma: no cover
            chunk = b""
        if not chunk:
            return b"".join(chunks), False
        if size + len(chunk) >= cap:
            chunks.append(chunk[: cap - size])
            if on_overflow is not None:
                on_overflow()
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)


def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill the child's whole process group, not just the child."""
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _collect(
    process: asyncio.subprocess.Process, limits: Limits
) -> tuple[bytes, bytes, bool]:
    """Read both streams under one deadline, killing the child on overflow."""

    def overflow() -> None:
        _terminate(process)

    stdout, stderr = await asyncio.wait_for(
        asyncio.gather(
            _drain(process.stdout, limits.max_output_bytes, overflow),
            _drain(process.stderr, max(4096, limits.max_output_bytes // 4), overflow),
        ),
        timeout=limits.timeout,
    )
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=5)
    return stdout[0], stderr[0], stdout[1] or stderr[1]


async def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    limits: Limits | None = None,
    allow_tests: bool = False,
) -> CommandResult:
    """Execute an allowlisted command with a scrubbed environment and hard limits."""
    limits = limits or Limits()
    if not allowed(argv, allow_tests=allow_tests):
        raise AccessDenied(f"Command is not allowed: {argv[0]}")
    child_env = build_env(env, home=cwd)
    started = asyncio.get_running_loop().time()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_preexec(limits),
        )
    except FileNotFoundError:
        raise ProviderError(f"Command {argv[0]} is not available.") from None
    except PermissionError:
        raise ProviderError(f"Command {argv[0]} is not executable.") from None

    try:
        stdout, stderr, truncated = await _collect(process, limits)
    except TimeoutError:
        _terminate(process)
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise ProviderError(
            f"Command {argv[0]} exceeded its {limits.timeout:g}s budget and was killed."
        ) from None
    except asyncio.CancelledError:
        _terminate(process)
        raise

    duration = int((asyncio.get_running_loop().time() - started) * 1000)
    return CommandResult(
        argv=argv,
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        truncated=truncated,
        duration_ms=duration,
        limits=limits,
    )


# --------------------------------------------------------------------------- #
# Repository code execution
# --------------------------------------------------------------------------- #

#: Container runtimes DevLens will drive, in order of preference.
RUNTIMES = ("podman", "docker")

CONTAINER_FLAGS = (
    "--rm",
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=192",
    "--memory=1g",
    "--cpus=2",
    "--user=65534:65534",
    "--env-file=/dev/null",
)


def isolation_runtime() -> str | None:
    """The container runtime available for isolating repository code, if any.

    ``DEVLENS_SANDBOX_RUNTIME`` pins a specific one; ``none`` disables
    containerisation and accepts process-level limits only.
    """
    configured = os.environ.get("DEVLENS_SANDBOX_RUNTIME", "").strip().lower()
    if configured == "none":
        return None
    if configured:
        return configured if shutil.which(configured) else None
    for runtime in RUNTIMES:
        if shutil.which(runtime):
            return runtime
    return None


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def require_unprivileged() -> None:
    """Refuse to execute repository code as root with no container runtime.

    ``RLIMIT_NPROC`` does not apply to a process running as uid 0 on Linux, so
    the process limits below are not containment for a root user: a fork bomb
    from a repository's own test suite is unbounded. Rather than implying an
    isolation that is not there, DevLens refuses and says what to change.

    ``DEVLENS_ALLOW_ROOT_REPO_TESTS=1`` overrides it for an environment that is
    already disposable, such as a CI container. The override is recorded in
    every report's limitations.
    """
    if not running_as_root() or enabled_flag("DEVLENS_ALLOW_ROOT_REPO_TESTS"):
        return
    raise AccessDenied(
        "Refusing to execute repository code as root with no container runtime: "
        "process limits do not constrain a root user, so this would run "
        "unconfined. Install podman or docker, run DevLens as a non-root user, "
        "or set DEVLENS_ALLOW_ROOT_REPO_TESTS=1 if this host is disposable."
    )


def container_argv(
    runtime: str, image: str, workdir: Path, argv: list[str]
) -> list[str]:
    """Build the container invocation for a repository test command.

    Separated out so the flags can be asserted in a test without a daemon.
    """
    return [
        runtime,
        "run",
        *CONTAINER_FLAGS,
        "--volume",
        f"{workdir}:/work:ro",
        "--tmpfs",
        # A mount specification for the container's own /tmp, not a path on this
        # host: the read-only root needs somewhere writable for a test run.
        "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108
        "--workdir",
        "/work",
        image,
        *argv,
    ]


async def run_repository_code(
    argv: list[str],
    *,
    cwd: Path,
    guard,
    image: str | None = None,
    limits: Limits | None = None,
) -> CommandResult:
    """Run a command that comes from the repository under inspection.

    Requires the ``repository_code_execution`` capability, which is denied by
    default and separate from read access. When a container runtime is present
    the command is additionally confined with no network, a read-only mount,
    dropped capabilities and a pid limit; otherwise it runs with the process
    limits above and the report says so.

    Running as root with no container runtime is refused: see
    :func:`require_unprivileged`.
    """
    guard.require("repository_code_execution")
    limits = limits or Limits.for_tests()
    runtime = isolation_runtime()
    if runtime is None:
        require_unprivileged()
        return await run(argv, cwd=cwd, limits=limits, allow_tests=True)
    if not allowed(argv, allow_tests=True):
        raise AccessDenied(f"Command is not allowed: {argv[0]}")
    wrapped = container_argv(
        runtime, image or os.environ.get("DEVLENS_SANDBOX_IMAGE", "python:3.12-slim"), cwd, argv
    )
    started = asyncio.get_running_loop().time()
    try:
        process = await asyncio.create_subprocess_exec(
            *wrapped,
            env=build_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:  # pragma: no cover - runtime disappeared mid-flight
        return await run(argv, cwd=cwd, limits=limits, allow_tests=True)
    try:
        stdout, stderr, truncated = await _collect(process, limits)
    except TimeoutError:
        _terminate(process)
        raise ProviderError("Repository test run exceeded its budget.") from None
    return CommandResult(
        argv=wrapped,
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        truncated=truncated,
        duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
        limits=replace(limits),
    )
