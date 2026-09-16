"""Evidence integrity.

The central claim DevLens makes is that it will not present something as
supported unless it actually retrieved it. These tests hold that claim to
account from three directions: the model rejects a fabricated status, the
provider mapping downgrades every non-success, and a workflow run against
unreachable providers produces nothing supported and confirms nothing.

Regression coverage for DL-P0-002.
"""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from devlens.agent.workflows.platform import InvestigateWorkflow, ObserveWorkflow, classify
from devlens.domain import (
    EvidenceItem,
    Hypothesis,
    PlatformRequest,
    Provenance,
    ProviderResult,
)
from devlens.evidence import from_provider, observed, retrieved, settle
from devlens.guardrails import CapabilityGuard


def provenance(**overrides) -> Provenance:
    return Provenance(
        **{"retrieved": True, "captured_at": time.time(), "source": "test", **overrides}
    )


# --------------------------------------------------------------------------- #
# The model refuses to represent the bug
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["SUPPORTED", "CONFIRMED", "REJECTED"])
def test_assertive_status_requires_a_successful_retrieval(status):
    with pytest.raises(ValidationError, match="without a successful retrieval"):
        EvidenceItem(
            id="E-1",
            type="metric",
            source="Prometheus",
            observation="Provider could not be reached.",
            status=status,
            provenance=provenance(retrieved=False),
        )


def test_unknown_status_is_always_representable():
    item = EvidenceItem(
        id="E-1",
        type="metric",
        source="Prometheus",
        observation="Prometheus could not be reached.",
        status="UNKNOWN",
        provenance=provenance(retrieved=False, provider_status="UNAVAILABLE"),
    )
    assert item.status == "UNKNOWN"


def test_provenance_cannot_claim_retrieval_from_a_failed_provider():
    with pytest.raises(ValidationError, match="cannot claim retrieval"):
        provenance(retrieved=True, provider_status="UNAVAILABLE")


def test_an_image_can_never_confirm_or_reject():
    with pytest.raises(ValidationError, match="cannot confirm or reject"):
        EvidenceItem(
            id="E-2",
            type="image",
            source="screenshot.png",
            observation="Error dialog visible.",
            status="CONFIRMED",
            provenance=provenance(),
        )
    # The helper downgrades rather than raising, so a caller cannot force it.
    assert retrieved("image", "s.png", "Dialog visible.", status="CONFIRMED").status == "SUPPORTED"


# --------------------------------------------------------------------------- #
# Provider results map honestly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "kwargs"),
    [
        ("UNAVAILABLE", {"error": "connection refused"}),
        ("TIMEOUT", {"error": "deadline exceeded"}),
        ("DENIED", {"error": "not on the allowlist"}),
        ("NOT_CONFIGURED", {}),
        ("EMPTY", {}),
    ],
)
def test_every_non_success_provider_state_yields_unknown_evidence(status, kwargs):
    result = ProviderResult(provider="Prometheus", status=status, **kwargs)
    item = from_provider(result, "metric", "Metrics")
    assert item.status == "UNKNOWN"
    assert item.provenance.retrieved is False
    assert item.provenance.provider_status == status


def test_an_available_provider_with_rows_supports():
    result = ProviderResult(
        provider="Loki", status="AVAILABLE", rows=["timeout in checkout"], query="{job}"
    )
    item = from_provider(result, "log", "Logs")
    assert item.status == "SUPPORTED"
    assert item.provenance.retrieved is True
    assert "timeout in checkout" in item.observation


def test_empty_is_distinguished_from_unavailable_in_the_wording():
    empty = from_provider(ProviderResult(provider="Loki", status="EMPTY"), "log", "Logs")
    down = from_provider(
        ProviderResult(provider="Loki", status="UNAVAILABLE", error="refused"),
        "log",
        "Logs",
    )
    assert "returned no matching data" in empty.observation
    assert "not evidence of absence" in empty.observation
    assert "could not be reached" in down.observation


def test_a_provider_result_cannot_be_available_without_rows():
    with pytest.raises(ValidationError):
        ProviderResult(provider="Loki", status="AVAILABLE", rows=[])
    with pytest.raises(ValidationError):
        ProviderResult(provider="Loki", status="EMPTY", rows=["x"])
    with pytest.raises(ValidationError):
        ProviderResult(provider="Loki", status="UNAVAILABLE")


# --------------------------------------------------------------------------- #
# Hypotheses are settled from evidence, never asserted
# --------------------------------------------------------------------------- #


def test_unknown_evidence_never_moves_a_hypothesis():
    items = [
        observed("metric", "Prometheus", "unreachable"),
        observed("log", "Loki", "unreachable"),
    ]
    settled = settle(
        Hypothesis(
            id="H1",
            statement="A pool is saturated.",
            supporting_evidence=[item.id for item in items],
        ),
        items,
    )
    assert settled.status == "open"
    assert settled.confidence == "LOW"
    assert settled.supporting_evidence == []


def test_code_evidence_supports_but_cannot_confirm():
    items = [
        retrieved("code", "a.py", "no timeout on the client", status="CONFIRMED"),
        retrieved("git", "log", "changed last week", status="CONFIRMED"),
    ]
    settled = settle(
        Hypothesis(
            id="H1",
            statement="A client has no timeout.",
            supporting_evidence=[item.id for item in items],
        ),
        items,
    )
    assert settled.status == "supported"
    assert settled.confidence == "HIGH"


def test_runtime_evidence_confirms():
    items = [retrieved("metric", "Prometheus", "p99 tracks pool saturation", status="CONFIRMED")]
    settled = settle(
        Hypothesis(id="H1", statement="A pool is saturated.", supporting_evidence=[items[0].id]),
        items,
    )
    assert settled.status == "confirmed"
    assert settled.confidence == "VERY_HIGH"


def test_contradicting_confirmed_evidence_rejects():
    against = retrieved("metric", "Prometheus", "pool never exceeded 12%", status="CONFIRMED")
    support = retrieved("code", "a.py", "pool size is 10")
    settled = settle(
        Hypothesis(
            id="H1",
            statement="A pool is saturated.",
            supporting_evidence=[support.id],
            contradicting_evidence=[against.id],
        ),
        [support, against],
    )
    assert settled.status == "rejected"
    assert settled.confidence == "VERY_HIGH"


def test_a_hypothesis_cannot_be_declared_supported_without_evidence():
    with pytest.raises(ValidationError, match="no supporting evidence"):
        Hypothesis(id="H1", statement="x", status="supported")
    with pytest.raises(ValidationError, match="no contradicting evidence"):
        Hypothesis(id="H1", statement="x", status="rejected")
    with pytest.raises(ValidationError, match="cannot carry high confidence"):
        Hypothesis(id="H1", statement="x", status="open", confidence="HIGH")


# --------------------------------------------------------------------------- #
# End to end: the exact scenario from the audit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unreachable_providers_do_not_support_a_hypothesis(monkeypatch):
    """The regression test for DL-P0-002.

    Three providers are configured and pointed at a closed port. Previously
    every connection error became SUPPORTED evidence, the hypothesis was
    promoted, and the summary claimed live telemetry.
    """
    monkeypatch.setenv("DEVLENS_LOKI_URL", "http://127.0.0.1:9/")
    monkeypatch.setenv("DEVLENS_PROM_URL", "http://127.0.0.1:9/")
    monkeypatch.setenv("DEVLENS_SQL_URL", "http://127.0.0.1:9/")
    guard = CapabilityGuard.from_env()
    assert "observability_provider" in guard.enabled

    report = await InvestigateWorkflow(guard).run(
        PlatformRequest(question="checkout p99 latency spiked after the deploy")
    )

    assert all(item.status == "UNKNOWN" for item in report.evidence_items)
    assert all(h.status == "open" for h in report.hypotheses)
    assert report.root_cause is None
    assert report.root_cause_status == "UNKNOWN"
    assert "could not be reached" in report.executive_summary
    assert any("unreachable" in item or "not configured" in item for item in report.unknowns)
    statuses = {result.status for result in report.provider_results}
    assert "AVAILABLE" not in statuses


@pytest.mark.asyncio
async def test_investigation_without_telemetry_says_nothing_is_confirmed():
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="checkout requests are timing out")
    )
    assert report.root_cause is None
    assert "nothing is confirmed" in report.executive_summary.lower() or any(
        "not configured" in item for item in report.unknowns
    )


@pytest.mark.asyncio
async def test_investigation_produces_differentiated_hypotheses():
    """Not one hard-coded sentence: a ranked set, each with an experiment."""
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="why do we see duplicate charges on retry?")
    )
    statements = [item.statement for item in report.hypotheses]
    assert len(statements) >= 3
    assert len(set(statements)) == len(statements)
    assert all(item.next_experiment for item in report.hypotheses)
    assert any("idempoten" in item.lower() for item in statements)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("p99 latency has tripled", "latency"),
        ("we are seeing 500s from the API", "failure"),
        ("the worker is OOMing", "resource"),
        ("customers get charged twice", "correctness"),
        ("requests return 403 unexpectedly", "authorization"),
        ("something feels off", "unclassified"),
    ],
)
def test_symptoms_are_classified(question, expected):
    category, mechanisms = classify(question)
    assert category == expected
    assert len(mechanisms) >= 2


@pytest.mark.asyncio
async def test_observe_reports_gaps_as_findings(git_repo):
    report = await ObserveWorkflow(CapabilityGuard()).run(
        PlatformRequest(path=str(git_repo))
    )
    assert report.findings, "a repository with no telemetry should produce gap findings"
    assert all(item.category == "observability" for item in report.findings)
    assert all(item.verification for item in report.findings)
