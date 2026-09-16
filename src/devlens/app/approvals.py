"""Proposals and approvals.

An approval authorises **one proposal**, not an action name. The proposal
records the exact payload, the run that produced it and why; the approval
records the hash of that payload. At execution the hash is re-checked, so an
approved comment cannot become a different comment, and an approved branch
cannot become a pull request.

Two races are closed here:

* **Double execution.** The transition from ``pending`` to ``approving`` is a
  conditional ``UPDATE ... WHERE status='pending'``. SQLite makes that atomic,
  so of two concurrent approvals exactly one claims the record and the other is
  told the approval is no longer pending — before either reaches the network.
* **Stale approval.** Every approval expires. An expired record cannot be
  executed and says so.

Records are persisted, so an approval queue survives a restart.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from devlens.app.store import Database
from devlens.domain import (
    AccessDenied,
    ApprovalRecord,
    Conflict,
    Proposal,
    ProposalCreate,
    ProviderError,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    action TEXT NOT NULL,
    repository TEXT,
    ticket_key TEXT,
    target TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    run_id TEXT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    decided_by TEXT,
    result TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    executed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_approvals_proposal ON approvals(proposal_id);
CREATE INDEX IF NOT EXISTS ix_proposals_run ON proposals(run_id, created_at DESC);
"""

DEFAULT_TTL = 3600.0
MAX_PENDING = 100


class ApprovalService:
    """Durable proposal and approval queue with single-execution guarantees."""

    def __init__(self, path: Path, ttl: float = DEFAULT_TTL, audit=None):
        self.db = Database(path, SCHEMA)
        self.ttl = ttl
        self.audit = audit
        self._locks: dict[str, asyncio.Lock] = {}

    # -- proposals --------------------------------------------------------- #

    def propose(self, proposal: Proposal) -> Proposal:
        with self.db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    proposal.id,
                    proposal.run_id,
                    proposal.action,
                    proposal.repository,
                    proposal.ticket_key,
                    proposal.target,
                    json.dumps(proposal.payload),
                    proposal.rationale,
                    proposal.payload_sha256,
                    proposal.created_at,
                ),
            )
        self._audit("proposal.created", proposal_id=proposal.id, action=proposal.action)
        return proposal

    def create_proposal(self, body: ProposalCreate) -> Proposal:
        return self.propose(
            Proposal(
                id=str(uuid4()),
                run_id=body.run_id,
                action=body.action,
                repository=body.repository,
                ticket_key=body.ticket_key,
                target=body.target,
                payload=body.payload,
                rationale=body.rationale,
                created_at=time.time(),
            )
        )

    def proposal(self, proposal_id: str) -> Proposal | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return _proposal(row) if row else None

    def proposals_for(self, run_id: str) -> list[Proposal]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE run_id = ? ORDER BY created_at DESC LIMIT 100",
                (run_id,),
            ).fetchall()
        return [
            proposal
            for row in rows
            if (proposal := _safe_proposal(row)) is not None
        ]

    # -- approvals --------------------------------------------------------- #

    def request(self, proposal_id: str, requested_by: str = "operator") -> ApprovalRecord:
        proposal = self.proposal(proposal_id)
        if proposal is None:
            raise ProviderError("No such proposal.")
        self.expire_due()
        with self.db.connect() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE status IN ('pending','approving')"
            ).fetchone()[0]
        if pending >= MAX_PENDING:
            raise ProviderError("Approval queue is full; decide the pending items first.")
        now = time.time()
        record = ApprovalRecord(
            id=str(uuid4()),
            proposal_id=proposal.id,
            run_id=proposal.run_id,
            action=proposal.action,
            target=proposal.target,
            payload_sha256=proposal.payload_sha256,
            status="pending",
            requested_by=requested_by,
            created_at=now,
            expires_at=now + self.ttl,
        )
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.proposal_id,
                    record.run_id,
                    record.action,
                    record.target,
                    record.payload_sha256,
                    record.status,
                    record.requested_by,
                    None,
                    None,
                    record.created_at,
                    record.expires_at,
                    None,
                    None,
                ),
            )
        self._audit(
            "approval.requested",
            approval_id=record.id,
            proposal_id=proposal.id,
            action=record.action,
            target=record.target,
        )
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return _approval(row) if row else None

    def list(self, limit: int = 100) -> list[ApprovalRecord]:
        self.expire_due()
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [_approval(row) for row in rows]

    def expire_due(self) -> int:
        with self.db.write() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status='expired', decided_at=? "
                "WHERE status='pending' AND expires_at <= ?",
                (time.time(), time.time()),
            )
            return cursor.rowcount

    def _claim(self, approval_id: str) -> ApprovalRecord:
        """Atomically move pending -> approving, or explain why we cannot.

        The conditional UPDATE is the single-execution guarantee: SQLite applies
        it atomically, so two concurrent approvals cannot both claim the record.
        """
        with self.db.write() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET status='approving' "
                "WHERE id=? AND status='pending' AND expires_at > ?",
                (approval_id, time.time()),
            )
            claimed = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise ProviderError("Approval not found.")
        record = _approval(row)
        if not claimed:
            if record.status == "expired" or record.expires_at <= time.time():
                raise Conflict("This approval expired; request it again.")
            raise Conflict(f"Approval is already {record.status}.")
        return record

    def _finish(self, approval_id: str, status: str, result: str, by: str) -> ApprovalRecord:
        now = time.time()
        with self.db.write() as conn:
            conn.execute(
                "UPDATE approvals SET status=?, result=?, decided_by=?, decided_at=?, "
                "executed_at=? WHERE id=?",
                (status, result, by, now, now if status == "approved" else None, approval_id),
            )
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return _approval(row)

    async def approve(
        self, approval_id: str, gateway, decided_by: str = "operator"
    ) -> ApprovalRecord:
        """Claim, re-validate, execute, record. In that order, exactly once."""
        self.expire_due()
        lock = self._locks.setdefault(approval_id, asyncio.Lock())
        async with lock:
            record = self._claim(approval_id)
            try:
                proposal = self.proposal(record.proposal_id)
            except Conflict as exc:
                outcome = self._finish(approval_id, "rejected", str(exc), decided_by)
                self._audit(
                    "approval.failed", approval_id=approval_id, outcome=str(exc)
                )
                return outcome
            if proposal is None:  # pragma: no cover - foreign key prevents this
                return self._finish(
                    approval_id, "rejected", "Proposal no longer exists.", decided_by
                )
            if gateway is None or not gateway.allowed:
                outcome = self._finish(
                    approval_id,
                    "approved",
                    "Approved. Write-back is disabled, so nothing was sent to the "
                    "remote. Set DEVLENS_ALLOW_WRITES=1 to execute approvals.",
                    decided_by,
                )
                self._audit(
                    "approval.approved",
                    approval_id=approval_id,
                    executed=False,
                    action=record.action,
                )
                return outcome
            try:
                result = await gateway.execute(proposal, record.payload_sha256)
                status = "approved"
            except (AccessDenied, Conflict) as exc:
                result, status = str(exc), "rejected"
            except ProviderError as exc:
                result, status = f"Remote write failed: {exc}", "rejected"
            outcome = self._finish(approval_id, status, result, decided_by)
            self._audit(
                "approval.executed" if status == "approved" else "approval.failed",
                approval_id=approval_id,
                proposal_id=proposal.id,
                action=record.action,
                target=record.target,
                outcome=result,
            )
            return outcome

    def reject(self, approval_id: str, decided_by: str = "operator") -> ApprovalRecord | None:
        record = self.get(approval_id)
        if record is None:
            return None
        if record.status != "pending":
            raise Conflict(f"Approval is already {record.status}.")
        outcome = self._finish(
            approval_id, "rejected", "Rejected by operator.", decided_by
        )
        self._audit("approval.rejected", approval_id=approval_id, action=record.action)
        return outcome

    def _audit(self, event: str, **fields) -> None:
        # The parameter is named ``event``, not ``action``: callers pass the
        # proposal's own ``action=`` as a field, and a collision here silently
        # broke every audited call path.
        if self.audit is not None:
            self.audit.record({"event": event, **fields})


def _proposal(row) -> Proposal:
    """Rebuild a proposal, refusing one whose stored payload was tampered with.

    ``Proposal`` re-derives the seal on construction, so a row edited behind
    DevLens's back fails validation here. That is the right answer, but it has
    to arrive as a ``Conflict`` the approval path can record — not as an
    unhandled validation error surfacing as a 500.
    """
    try:
        return _build(row)
    except ValidationError as exc:
        raise Conflict(
            "The stored proposal changed after it was approved; approve it again."
        ) from exc


def _build(row) -> Proposal:
    return Proposal(
        id=row["id"],
        run_id=row["run_id"],
        action=row["action"],
        repository=row["repository"],
        ticket_key=row["ticket_key"],
        target=row["target"],
        payload=json.loads(row["payload_json"]),
        rationale=row["rationale"],
        created_at=row["created_at"],
        payload_sha256=row["payload_sha256"],
    )


def _approval(row) -> ApprovalRecord:
    return ApprovalRecord(
        id=row["id"],
        proposal_id=row["proposal_id"],
        run_id=row["run_id"],
        action=row["action"],
        target=row["target"],
        payload_sha256=row["payload_sha256"],
        status=row["status"],
        requested_by=row["requested_by"],
        decided_by=row["decided_by"],
        result=row["result"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        executed_at=row["executed_at"],
    )


def _safe_proposal(row) -> Proposal | None:
    """A listing skips a tampered row rather than failing the whole page."""
    try:
        return _proposal(row)
    except Conflict:
        return None
