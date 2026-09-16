"""Durable operator state: knowledge notes and onboarding progress.

These were in-memory in the local application, so a restart silently discarded
an operator's notes. They are now backed by the same SQLite machinery as jobs,
with the in-memory classes kept as the interface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from devlens.app.catalog import (
    KnowledgeCreate,
    KnowledgeDocument,
    KnowledgeStore,
    OnboardingStore,
)
from devlens.app.store import Database
from devlens.domain import ProviderError

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
"""

MAX_DOCUMENTS = 1000


class StateRepository(Protocol):
    def read(self, key: str, default: Any) -> Any: ...
    def write(self, key: str, value: Any) -> None: ...


class SQLiteStateRepository:
    def __init__(self, path: Path):
        self.db = Database(path, SCHEMA)

    def read(self, key: str, default: Any) -> Any:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:  # pragma: no cover - corrupted row
            return default

    def write(self, key: str, value: Any) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, default=str)),
            )


class PersistentKnowledgeStore(KnowledgeStore):
    def __init__(self, repository: StateRepository):
        self.repository = repository
        self.documents = [
            KnowledgeDocument.model_validate(item)
            for item in repository.read("knowledge", [])
        ]

    def create(self, body: KnowledgeCreate) -> KnowledgeDocument:
        if len(self.documents) >= MAX_DOCUMENTS:
            raise ProviderError("Knowledge capacity reached.")
        record = super().create(body)
        try:
            self.repository.write(
                "knowledge", [item.model_dump() for item in self.documents]
            )
        except Exception:
            self.documents.pop()
            raise
        return record


class PersistentOnboardingStore(OnboardingStore):
    def __init__(self, repository: StateRepository):
        self.repository = repository
        self.completed = list(repository.read("onboarding", []))

    def mark(self, step: str) -> dict:
        previous = list(self.completed)
        result = super().mark(step)
        try:
            self.repository.write("onboarding", self.completed)
        except Exception:
            self.completed = previous
            raise
        return result
