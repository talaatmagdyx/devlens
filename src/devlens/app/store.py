"""SQLite plumbing shared by the job, run and approval stores.

SQLite is the right database for a single-node operator tool; it just has to be
configured for the access pattern. Three settings do most of the work:

* ``journal_mode=WAL`` so readers never block the writer. Without it, polling a
  run's event stream blocks the worker writing to it.
* ``busy_timeout`` measured in seconds rather than milliseconds of patience.
* ``synchronous=NORMAL``, which is durable under process crash — the failure
  mode that matters here — without paying for an fsync per statement.

Connections are per-thread and reused rather than opened per statement.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from devlens.domain import StorageUnavailable

BUSY_TIMEOUT_MS = 10_000


class Database:
    """A WAL-mode SQLite database with one connection per thread."""

    def __init__(
        self,
        path: Path,
        schema: str,
        *,
        migrate: Callable[[sqlite3.Connection], None] | None = None,
    ):
        self.path = Path(path)
        self._ensure_writable()
        self._local = threading.local()
        self._schema = schema
        try:
            with self.connect() as conn:
                if migrate is not None:
                    migrate(conn)
                conn.executescript(schema)
        except sqlite3.OperationalError as exc:
            raise StorageUnavailable(self._explain(exc)) from None

    def _ensure_writable(self) -> None:
        """Fail with an actionable message rather than a sqlite traceback.

        The case this exists for: a container image chowns ``/data`` at build
        time, the operator mounts a fresh tmpfs over it at run time, and the
        process — running as an unprivileged user — cannot create its database.
        The raw error is ``unable to open database file``, which says nothing
        about the directory, the user, or the mount.
        """
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageUnavailable(self._explain(exc)) from None
        if not os.access(parent, os.W_OK | os.X_OK):
            raise StorageUnavailable(self._explain(None))

    def _explain(self, exc: BaseException | None) -> str:
        parent = self.path.parent
        who = f"uid {os.getuid()}" if hasattr(os, "getuid") else "this process"
        detail = f" ({type(exc).__name__}: {exc})" if exc is not None else ""
        return (
            f"DevLens cannot open its database at {self.path}{detail}. "
            f"The directory {parent} must exist and be writable by {who}. "
            "In a container this usually means a volume or tmpfs was mounted "
            "over a directory the image had prepared: mount it writable by that "
            "uid, or point DEVLENS_JOBS_DB somewhere else."
        )

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL is a strong preference, not a requirement: read-only media rejects
        # it and the database still works without it.
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA journal_mode = WAL")

    @property
    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
            )
            self._configure(conn)
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Read-only or single-statement access."""
        yield self._connection

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """An immediate transaction, committed on success and rolled back on error."""
        conn = self._connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
