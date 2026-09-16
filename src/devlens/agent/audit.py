"""Append-only audit trail with defensive redaction.

Redaction matches on substrings rather than exact key names, because the set of
credential-shaped keys grows over time: ``token`` catches ``access_token``,
``refresh_token`` and ``github_token`` without anyone having to remember to add
them. URLs are redacted separately, since credentials hide in userinfo and query
strings where no key name appears at all.

The file is opened per write in append mode with an exclusive lock, so
concurrent writers interleave whole records rather than fragments.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

#: A key containing any of these is redacted, wherever it appears in the tree.
SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "key",
    "auth",
    "cookie",
    "credential",
    "session",
    "signature",
    "private",
)

#: Keys that contain a marker but are not secrets. Without this, "key" would
#: redact ordinary identifiers.
SAFE_KEYS = frozenset(
    {"key", "ticket_key", "idempotency_key", "keys", "primary_key", "sort_key"}
)

REDACTED = "***"
MAX_RECORDS = 500
MAX_BYTES = 8 * 1024 * 1024

_SECRET_VALUE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9\-_]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)


def is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in SAFE_KEYS:
        return False
    return any(marker in lowered for marker in SECRET_MARKERS)


def redact_url(value: str) -> str:
    """Strip userinfo and credential-shaped query parameters from a URL."""
    try:
        parts = urlsplit(value)
    except ValueError:  # pragma: no cover - urlsplit is extremely permissive
        return value
    if not parts.scheme or not parts.netloc:
        return value
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username or parts.password:
        netloc = f"{REDACTED}@{netloc}"
    query = parts.query
    if query:
        pairs = []
        for pair in query.split("&"):
            name, _, _ = pair.partition("=")
            pairs.append(f"{name}={REDACTED}" if is_secret_key(name) else pair)
        query = "&".join(pairs)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact(value: Any) -> Any:
    """Recursively remove credentials from an arbitrary structure."""
    if isinstance(value, dict):
        return {
            key: REDACTED if is_secret_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if "://" in value:
            return _SECRET_VALUE.sub(REDACTED, redact_url(value))
        return _SECRET_VALUE.sub(REDACTED, value)
    return value


class AuditLog:
    """Structured, redacted, bounded audit trail.

    Durability is a file append under an exclusive lock. That is genuinely
    append-oriented but not tamper-proof: anyone who can write the file can
    rewrite it. The README states that rather than implying more.
    """

    def __init__(self, path: Path | None = None, max_records: int = MAX_RECORDS):
        self.path = path
        self.records: deque[dict] = deque(maxlen=max_records)

    @classmethod
    def from_env(cls) -> AuditLog:
        raw = os.environ.get("DEVLENS_AUDIT_LOG")
        return cls(Path(raw) if raw else None)

    def record(self, event: dict) -> dict:
        entry = {"id": str(uuid4()), "ts": time.time(), **redact(event)}
        self.records.append(entry)
        if self.path is not None:
            self._append(entry)
        return entry

    def _append(self, entry: dict) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate()
        line = json.dumps(entry, default=str) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _rotate(self) -> None:
        assert self.path is not None
        try:
            if self.path.stat().st_size < MAX_BYTES:
                return
        except OSError:
            return
        self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))

    def recent(self, limit: int = 50) -> list[dict]:
        """The tail of the audit trail, read without loading the whole file."""
        limit = max(1, min(limit, 200))
        if self.path is None or not self.path.exists():
            return list(self.records)[-limit:]
        return [
            entry
            for line in _tail(self.path, limit)
            if (entry := _parse(line)) is not None
        ]


def _tail(path: Path, limit: int) -> list[str]:
    """Read the last ``limit`` lines without reading the whole file."""
    try:
        size = path.stat().st_size
    except OSError:  # pragma: no cover - raced with rotation
        return []
    block = 64 * 1024
    with path.open("rb") as handle:
        buffer = b""
        offset = size
        while offset > 0 and buffer.count(b"\n") <= limit:
            step = min(block, offset)
            offset -= step
            handle.seek(offset)
            buffer = handle.read(step) + buffer
    lines = buffer.decode("utf-8", errors="replace").splitlines()
    return lines[-limit:]


def _parse(line: str) -> dict | None:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None
