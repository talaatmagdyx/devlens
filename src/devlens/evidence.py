"""Construction and settlement rules for evidence and hypotheses.

Workflows never set an evidence status or a hypothesis status by hand. They
record what happened — a file was read, a provider answered, a provider was
unreachable — and these helpers derive the status from that record. Every rule
in this module is deterministic and stated in code; none of it is a model
judgement and none of it produces a percentage.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from itertools import count

from devlens.domain import (
    ConfidenceBand,
    EvidenceItem,
    EvidenceKind,
    Hypothesis,
    Provenance,
    ProviderResult,
)

#: Evidence types that describe intent rather than runtime behaviour. On their
#: own these can support a hypothesis but never confirm one, because source code
#: and screenshots show what should happen, not what did.
STATIC_KINDS: frozenset[str] = frozenset({"code", "git", "jira", "image", "document"})

#: Evidence types that observe the system actually running.
RUNTIME_KINDS: frozenset[str] = frozenset(
    {"log", "metric", "trace", "sql", "test", "benchmark"}
)

_counter = count(1)


def new_id(prefix: str = "E") -> str:
    return f"{prefix}-{next(_counter):04d}"


def reset_ids() -> None:
    """Restart id allocation. Used by tests to keep ids stable."""
    global _counter
    _counter = count(1)


def now() -> float:
    return time.time()


def observed(
    kind: EvidenceKind,
    source: str,
    observation: str,
    *,
    reason: str | None = None,
    provider: str | None = None,
    provider_status: str | None = None,
    run_id: str | None = None,
) -> EvidenceItem:
    """Record something DevLens noticed but did not verify.

    Always ``UNKNOWN``. This is the only way to represent an unavailable
    provider, an empty result set or an un-followed lead.
    """
    return EvidenceItem(
        id=new_id(),
        type=kind,
        source=source,
        observation=observation if reason is None else f"{observation} ({reason})",
        status="UNKNOWN",
        provenance=Provenance(
            retrieved=False,
            captured_at=now(),
            source=source,
            provider=provider,
            provider_status=provider_status,  # type: ignore[arg-type]
            run_id=run_id,
        ),
    )


def retrieved(
    kind: EvidenceKind,
    source: str,
    observation: str,
    *,
    status: str = "SUPPORTED",
    excerpt: str | None = None,
    selectors: Iterable[str] = (),
    run_id: str | None = None,
    **provenance: object,
) -> EvidenceItem:
    """Record material DevLens actually read, with its provenance.

    ``status`` defaults to ``SUPPORTED``; pass ``CONFIRMED`` only where the
    retrieval itself settles the question.
    """
    if kind == "image" and status in {"CONFIRMED", "REJECTED"}:
        status = "SUPPORTED"
    return EvidenceItem(
        id=new_id(),
        type=kind,
        source=source,
        observation=observation,
        status=status,  # type: ignore[arg-type]
        excerpt=excerpt,
        selectors=list(selectors)[:64],
        provenance=Provenance(
            retrieved=True,
            captured_at=now(),
            source=source,
            run_id=run_id,
            **provenance,  # type: ignore[arg-type]
        ),
    )


def from_provider(
    result: ProviderResult,
    kind: EvidenceKind,
    label: str,
    *,
    run_id: str | None = None,
) -> EvidenceItem:
    """Turn one provider outcome into exactly one honest evidence item.

    This is the mapping that used to file connection errors as support:

    ==================  ==========================================  ==========
    Provider status     Observation                                 Evidence
    ==================  ==========================================  ==========
    AVAILABLE           the rows that came back                     SUPPORTED
    EMPTY               "queried, returned no matching data"        UNKNOWN
    NOT_CONFIGURED      "not configured"                            UNKNOWN
    UNAVAILABLE         "could not be reached: <reason>"            UNKNOWN
    TIMEOUT             "did not answer within the budget"          UNKNOWN
    DENIED              "refused the query: <reason>"               UNKNOWN
    ==================  ==========================================  ==========
    """
    if result.usable:
        rows = "; ".join(result.rows[:3])
        suffix = " (truncated)" if result.truncated else ""
        return EvidenceItem(
            id=new_id(),
            type=kind,
            source=result.provider,
            observation=f"{label}: {rows}{suffix}",
            status="SUPPORTED",
            selectors=[result.query] if result.query else [],
            provenance=Provenance(
                retrieved=True,
                captured_at=now(),
                source=result.provider,
                provider=result.provider,
                provider_status="AVAILABLE",
                query=result.query,
                window_start=result.window_start,
                window_end=result.window_end,
                run_id=run_id,
            ),
        )
    reasons = {
        "EMPTY": f"{label} UNKNOWN: {result.provider} was queried and returned no "
        "matching data. Absence of data is not evidence of absence.",
        "NOT_CONFIGURED": f"{label} UNKNOWN: {result.provider} is not configured. "
        "Runtime verification cannot run.",
        "UNAVAILABLE": f"{label} UNKNOWN: {result.provider} could not be reached "
        f"({result.error}). This is a gap in the investigation, not a finding.",
        "TIMEOUT": f"{label} UNKNOWN: {result.provider} did not answer within its "
        f"time budget ({result.error}).",
        "DENIED": f"{label} UNKNOWN: {result.provider} refused the query "
        f"({result.error}).",
    }
    return EvidenceItem(
        id=new_id(),
        type=kind,
        source=result.provider,
        observation=reasons[result.status],
        status="UNKNOWN",
        provenance=Provenance(
            retrieved=False,
            captured_at=now(),
            source=result.provider,
            provider=result.provider,
            provider_status=result.status,
            query=result.query,
            run_id=run_id,
        ),
    )


def _weight(item: EvidenceItem) -> int:
    return 2 if item.status == "CONFIRMED" else 1


def settle(
    hypothesis: Hypothesis, items: Iterable[EvidenceItem]
) -> Hypothesis:
    """Derive a hypothesis's status and confidence band from its evidence.

    The rules, in order:

    1. Any contradicting ``CONFIRMED`` evidence rejects it (``VERY_HIGH``).
    2. A ``CONFIRMED`` runtime observation with nothing against it confirms it
       (``VERY_HIGH``). Code, git, Jira and image evidence cannot confirm on
       their own — they show intent, not behaviour.
    3. Supporting evidence from two or more distinct sources supports it
       (``HIGH``); from one source, ``MEDIUM``.
    4. Otherwise it stays open at ``LOW``.

    Evidence that is ``UNKNOWN`` never counts in either direction, which is why
    an unreachable provider can no longer move a hypothesis.
    """
    index = {item.id: item for item in items}
    supporting = [
        index[ref]
        for ref in hypothesis.supporting_evidence
        if ref in index and index[ref].status in {"CONFIRMED", "SUPPORTED"}
    ]
    against = [
        index[ref]
        for ref in hypothesis.contradicting_evidence
        if ref in index and index[ref].status in {"CONFIRMED", "SUPPORTED", "REJECTED"}
    ]
    supporting_ids = [item.id for item in supporting]
    against_ids = [item.id for item in against]

    if any(item.status == "CONFIRMED" for item in against):
        return hypothesis.model_copy(
            update={
                "status": "rejected",
                "confidence": "VERY_HIGH",
                "supporting_evidence": supporting_ids,
                "contradicting_evidence": against_ids,
            }
        )
    runtime_confirmed = any(
        item.status == "CONFIRMED" and item.type in RUNTIME_KINDS
        for item in supporting
    )
    if runtime_confirmed and not against:
        return hypothesis.model_copy(
            update={
                "status": "confirmed",
                "confidence": "VERY_HIGH",
                "supporting_evidence": supporting_ids,
                "contradicting_evidence": against_ids,
            }
        )
    if supporting:
        distinct = {item.type for item in supporting}
        strength = sum(_weight(item) for item in supporting)
        band: ConfidenceBand = (
            "HIGH" if len(distinct) >= 2 and strength >= 3 and not against else "MEDIUM"
        )
        return hypothesis.model_copy(
            update={
                "status": "supported",
                "confidence": band,
                "supporting_evidence": supporting_ids,
                "contradicting_evidence": against_ids,
            }
        )
    return hypothesis.model_copy(
        update={
            "status": "open",
            "confidence": "LOW",
            "supporting_evidence": supporting_ids,
            "contradicting_evidence": against_ids,
        }
    )


def finding_band(status: str, corroborations: int) -> ConfidenceBand:
    """Confidence band for a finding, from its status and how many signals agree.

    Deterministic and deliberately coarse. There is no arithmetic here that
    could be mistaken for a calibrated probability.
    """
    if status == "CONFIRMED":
        return "VERY_HIGH"
    if status == "REJECTED":
        return "VERY_HIGH"
    if status == "HYPOTHESIS":
        return "MEDIUM" if corroborations >= 2 else "LOW"
    if status == "SUPPORTED":
        return "HIGH" if corroborations >= 2 else "MEDIUM"
    return "LOW"


def summarise(items: Iterable[EvidenceItem]) -> dict[str, int]:
    """Count evidence by status, for report headers and the UI."""
    counts = {
        "CONFIRMED": 0,
        "SUPPORTED": 0,
        "HYPOTHESIS": 0,
        "UNKNOWN": 0,
        "REJECTED": 0,
    }
    for item in items:
        counts[item.status] += 1
    return counts
