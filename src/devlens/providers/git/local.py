"""Local git checkout access.

Everything that touches the filesystem is either a bounded subprocess or is run
off the event loop with :func:`asyncio.to_thread`. A synchronous ``rglob`` over
a large working tree inside an ``async def`` blocks every other request in the
process, which is how a single analyze call used to stall the whole API.

Searches exclude vendored and generated directories explicitly rather than
relying on the target repository having a well-maintained ``.gitignore``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from devlens.domain import ProviderError
from devlens.sandbox.workspace import Workspace
from devlens.tools.shell import Limits, run

MAX_FILE_BYTES = 100_000
MAX_TREE_ENTRIES = 2_000
MAX_SEARCH_BYTES = 200_000

#: Never searched or listed: large, generated, or not the operator's code.
EXCLUDED_GLOBS = (
    "!.git",
    "!node_modules",
    "!vendor",
    "!dist",
    "!build",
    "!target",
    "!.venv",
    "!venv",
    "!__pycache__",
    "!*.min.js",
    "!*.lock",
    "!*.map",
)


class RepoWorkspace:
    """Read-only operations on a checked-out repository."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def tree(self) -> list[str]:
        """List tracked-looking files without blocking the event loop."""
        return await asyncio.to_thread(self._walk)

    def _walk(self) -> list[str]:
        root = self.workspace.root
        skip = {part.lstrip("!") for part in EXCLUDED_GLOBS if not part.startswith("!*")}
        found: list[str] = []
        stack = [root]
        while stack and len(found) < MAX_TREE_ENTRIES:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:  # pragma: no cover - unreadable directory
                continue
            for entry in entries:
                if entry.name in skip:
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        stack.append(entry)
                    elif entry.is_file():
                        found.append(str(entry.relative_to(root)))
                except OSError:  # pragma: no cover - raced with a writer
                    continue
                if len(found) >= MAX_TREE_ENTRIES:
                    break
        return sorted(found)

    async def read(self, path: str) -> str:
        target = self.workspace.resolve(path)
        data = await asyncio.to_thread(_read_bytes, target)
        if len(data) > MAX_FILE_BYTES or b"\x00" in data:
            raise ProviderError("Repository file is oversized, binary, or malformed.")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError("Repository file is not UTF-8 text.") from None

    async def search(self, query: str) -> str:
        """Search with explicit exclusions and a hard output ceiling."""
        # No -I: that suppresses the filename, and every consumer of this output
        # parses "path:line:text" to locate the hit. With it, every result was
        # silently discarded and a local search produced no evidence at all.
        argv = [
            "rg",
            "-n",
            "--with-filename",
            "--max-count",
            "20",
            "--max-filesize",
            "1M",
            "--no-messages",
        ]
        for glob in EXCLUDED_GLOBS:
            argv += ["--glob", glob]
        argv += ["--", query]
        try:
            result = await run(
                argv,
                cwd=self.workspace.root,
                limits=Limits(timeout=20.0, max_output_bytes=MAX_SEARCH_BYTES),
            )
        except ProviderError:
            result = await run(
                ["git", "grep", "-n", "-I", "--", query],
                cwd=self.workspace.root,
                limits=Limits(timeout=20.0, max_output_bytes=MAX_SEARCH_BYTES),
            )
        return result.stdout

    async def log(self, path: str | None = None) -> str:
        argv = ["git", "log", "-n", "20", "--oneline", "--no-color"]
        if path:
            argv += ["--", path]
        return (await run(argv, cwd=self.workspace.root)).stdout

    async def blame(self, path: str) -> str:
        result = await run(
            ["git", "blame", "--no-color", "--", path],
            cwd=self.workspace.root,
            limits=Limits(timeout=20.0, max_output_bytes=40_000),
        )
        return result.stdout

    async def show(self, ref: str = "HEAD") -> str:
        result = await run(
            ["git", "show", "--stat", "--no-color", ref],
            cwd=self.workspace.root,
            limits=Limits(timeout=20.0, max_output_bytes=40_000),
        )
        return result.stdout

    async def diff(self) -> str:
        result = await run(
            ["git", "diff", "--no-color"],
            cwd=self.workspace.root,
            limits=Limits(timeout=20.0, max_output_bytes=200_000),
        )
        return result.stdout

    async def status(self) -> str:
        return (
            await run(["git", "status", "--short"], cwd=self.workspace.root)
        ).stdout

    async def markers(self) -> dict[str, bool]:
        """Detect a test runner. Deliberately narrow: a bare ``pyproject.toml``
        is not evidence that a repository uses pytest."""
        return await asyncio.to_thread(self._markers)

    def _markers(self) -> dict[str, bool]:
        root = self.workspace.root
        pytest_markers = (
            (root / "pytest.ini").exists()
            or (root / "tox.ini").exists()
            or ((root / "setup.cfg").exists()
            and "pytest" in _safe_read(root / "setup.cfg"))
            or ((root / "pyproject.toml").exists()
            and "pytest" in _safe_read(root / "pyproject.toml"))
            or ((root / "tests").is_dir()
            and any(root.glob("tests/test_*.py")))
        )
        return {
            "pytest": bool(pytest_markers),
            "rspec": (root / "spec").is_dir() and (root / "Gemfile").exists(),
        }


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise ProviderError("Repository file could not be read.") from None


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:20_000]
    except OSError:  # pragma: no cover - unreadable config
        return ""
