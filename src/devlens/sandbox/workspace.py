"""Disposable clones of allowlisted repositories.

A workspace is a temporary directory holding a shallow clone with its remotes
removed. That makes it safe to *read*. It does not make it safe to *execute* —
isolation for repository code lives in :mod:`devlens.tools.shell`, behind the
``repository_code_execution`` capability. The two concerns are deliberately
separate so that neither is mistaken for the other.

The clone credential is supplied through ``GIT_CONFIG_*`` environment variables
for the duration of the clone and is never written into ``.git/config``, so a
token cannot survive in the workspace.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from devlens.domain import AccessDenied, ProviderError
from devlens.tools.shell import Limits, run

ALLOWED_HOSTS = frozenset({"github.com", "bitbucket.org"})
CLONE_DEPTH = 50
CLONE_TIMEOUT = 180.0


@dataclass(frozen=True)
class CloneSettings:
    host: str
    authorization_header: str


class Workspace:
    """A checked-out repository with containment for every path it hands out."""

    def __init__(self, root: Path, disposable: bool = False):
        self.root = root.resolve()
        self.disposable = disposable

    async def close(self) -> None:
        if self.disposable and self.root.exists():
            await asyncio.to_thread(shutil.rmtree, self.root, True)

    async def __aenter__(self) -> Workspace:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    def resolve(self, path: str) -> Path:
        """Resolve a path inside the workspace, refusing anything that escapes.

        Symlinks are resolved *before* containment is checked, which is the
        order that makes the check meaningful.
        """
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root):
            raise AccessDenied("Path escapes the workspace.")
        return target

    async def create_local_branch(self, name: str) -> None:
        if not name.startswith("devlens/"):
            raise AccessDenied("Sandbox branches must use the devlens/ prefix.")
        result = await run(["git", "checkout", "-B", name], cwd=self.root)
        if result.returncode != 0:
            raise ProviderError("Could not create a local sandbox branch.")


def open_local(path: str) -> Workspace:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ProviderError(f"{path} is not a directory.")
    if not (root / ".git").exists():
        raise ProviderError(f"{path} is not a git repository.")
    return Workspace(root, disposable=False)


async def clone_repository(
    settings: CloneSettings, repository: str, ref: str
) -> Workspace:
    if settings.host not in ALLOWED_HOSTS:
        raise AccessDenied(f"Clone host {settings.host!r} is not allowed.")
    if "/" not in repository or any(
        part in {"", ".", ".."} for part in repository.split("/")
    ):
        raise AccessDenied("Repository identifier is not valid for cloning.")
    dest = Path(tempfile.mkdtemp(prefix="devlens-"))
    url = f"https://{settings.host}/{repository}.git"
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: {settings.authorization_header}",
    }
    result = await run(
        ["git", "clone", "--depth", str(CLONE_DEPTH), "--no-tags", "--", url, str(dest)],
        env=env,
        limits=Limits(timeout=CLONE_TIMEOUT, max_output_bytes=64_000),
    )
    if result.returncode != 0:
        await asyncio.to_thread(shutil.rmtree, dest, True)
        raise ProviderError(f"Cloning {repository} failed.")
    if ref and ref not in {"HEAD", ""}:
        # "--" keeps a ref that begins with "-" from being read as an option.
        checkout = await run(["git", "checkout", "--detach", "--", ref], cwd=dest)
        if checkout.returncode != 0:
            checkout = await run(["git", "checkout", "--detach", ref], cwd=dest)
        if checkout.returncode != 0:
            await asyncio.to_thread(shutil.rmtree, dest, True)
            raise ProviderError(f"Could not check out {ref!r}.")
    await detach_remotes(dest)
    return Workspace(dest, disposable=True)


async def detach_remotes(root: Path) -> None:
    """Drop the origin remote so the workspace cannot reach the network."""
    await run(["git", "remote", "remove", "origin"], cwd=root)
