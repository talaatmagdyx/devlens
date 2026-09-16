"""Properties, checked against generated input.

Every other test in this suite names a case. These name an invariant and let
Hypothesis look for the case that breaks it — which is the right shape for the
audit's central claims, because "DevLens never overstates what it found" is a
statement about all inputs, not about the six an author thought of.

Each property below corresponds to a rule the audit required.
"""

from __future__ import annotations

import re
import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from devlens.agent.workflows.platform import (
    DesignWorkflow,
    InvestigateWorkflow,
    classify,
    extract_requirements,
)
from devlens.domain import (
    EvidenceItem,
    Hypothesis,
    PlatformRequest,
    Provenance,
    ProviderResult,
)
from devlens.evidence import from_provider, settle
from devlens.guardrails import CapabilityGuard

QUIET = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Arbitrary operator prose: words, punctuation, numbers, the occasional symptom
# keyword, and text that looks like an instruction.
QUESTIONS = st.text(
    alphabet=st.sampled_from(list(string.ascii_letters + string.digits + " .,?!-_/%'\"<>")),
    min_size=0,
    max_size=200,
)

PROVIDER_STATUSES = st.sampled_from(
    ["AVAILABLE", "EMPTY", "UNAVAILABLE", "TIMEOUT", "DENIED", "NOT_CONFIGURED"]
)


# --------------------------------------------------------------------------- #
# Evidence can never overstate its source
# --------------------------------------------------------------------------- #


@given(status=PROVIDER_STATUSES, rows=st.lists(st.text(max_size=40), max_size=5))
@QUIET
def test_only_an_available_provider_can_ever_support(status, rows):
    if status == "AVAILABLE" and not rows:
        return  # the model refuses this combination; covered directly elsewhere
    if status != "AVAILABLE" and rows:
        rows = []
    result = ProviderResult(
        provider="P",
        status=status,
        rows=rows,
        error="e" if status in {"UNAVAILABLE", "TIMEOUT", "DENIED"} else None,
    )
    item = from_provider(result, "metric", "Metrics")
    if status == "AVAILABLE":
        assert item.status == "SUPPORTED"
        assert item.provenance.retrieved is True
    else:
        assert item.status == "UNKNOWN"
        assert item.provenance.retrieved is False


@given(
    status=st.sampled_from(["CONFIRMED", "SUPPORTED", "REJECTED"]),
    source=st.text(min_size=1, max_size=30),
)
@QUIET
def test_an_assertive_status_always_needs_a_retrieval(status, source):
    with pytest.raises(ValidationError):
        EvidenceItem(
            id="E-1",
            type="metric",
            source=source,
            observation="o",
            status=status,
            provenance=Provenance(retrieved=False, captured_at=0.0, source=source),
        )


@given(
    kinds=st.lists(
        st.tuples(
            st.sampled_from(["code", "git", "metric", "log", "trace", "image", "sql"]),
            st.sampled_from(["CONFIRMED", "SUPPORTED", "HYPOTHESIS", "UNKNOWN"]),
        ),
        min_size=0,
        max_size=6,
    )
)
@QUIET
def test_a_hypothesis_is_never_settled_beyond_its_evidence(kinds):
    from devlens.evidence import observed, retrieved

    items = []
    for index, (kind, status) in enumerate(kinds):
        if status == "UNKNOWN":
            items.append(observed(kind, f"s{index}", "unreachable"))
        else:
            items.append(retrieved(kind, f"s{index}", "o", status=status))

    settled = settle(
        Hypothesis(id="H1", statement="x", supporting_evidence=[item.id for item in items]),
        items,
    )
    supporting = [item for item in items if item.status in {"CONFIRMED", "SUPPORTED"}]
    if not supporting:
        assert settled.status == "open"
        assert settled.confidence == "LOW"
    if settled.status == "confirmed":
        assert any(
            item.status == "CONFIRMED" and item.type in {"metric", "log", "trace", "sql", "test", "benchmark"}
            for item in supporting
        ), "only runtime evidence can confirm"
    if settled.status in {"supported", "confirmed"}:
        assert settled.supporting_evidence, "a settled hypothesis cites its evidence"


# --------------------------------------------------------------------------- #
# Investigation output, for any question at all
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@given(question=QUESTIONS)
@QUIET
async def test_every_question_yields_distinct_hypotheses_each_with_an_experiment(question):
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question=question)
    )
    statements = [item.statement for item in report.hypotheses]
    assert len(statements) >= 3, "even an unclassified symptom gets a differentiated set"
    assert len(set(statements)) == len(statements), "no hypothesis is repeated"
    assert all(item.next_experiment for item in report.hypotheses)
    assert all(item.confidence in {"LOW", "MEDIUM", "HIGH", "VERY_HIGH"} for item in report.hypotheses)


@pytest.mark.asyncio
@given(question=QUESTIONS)
@QUIET
async def test_no_root_cause_without_confirming_evidence(question):
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question=question)
    )
    confirmed = [item for item in report.evidence_items if item.status == "CONFIRMED"]
    if not confirmed:
        assert report.root_cause is None
        assert report.root_cause_status in {"UNKNOWN", "HYPOTHESIS"}


PERCENT = re.compile(r"\b\d{1,3}(\.\d+)?\s?%")


@pytest.mark.asyncio
@given(question=QUESTIONS)
@QUIET
async def test_no_report_ever_expresses_certainty_as_a_percentage(question):
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question=question)
    )
    rendered = report.model_dump_json()
    for match in PERCENT.finditer(rendered):
        window = rendered[max(0, match.start() - 60) : match.end() + 20].lower()
        assert not any(
            word in window for word in ("confidence", "certain", "sure", "likelihood", "probability")
        ), f"a percentage is being used as certainty: {window}"


@pytest.mark.asyncio
@given(question=QUESTIONS)
@QUIET
async def test_a_question_is_never_echoed_back_as_a_finding(question):
    """The original bug: one hypothesis with the question pasted into it."""
    stripped = question.strip()
    if len(stripped) < 12:
        return
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question=question)
    )
    assert all(stripped not in item.statement for item in report.hypotheses)


@given(question=QUESTIONS)
@QUIET
def test_classification_always_returns_a_usable_set(question):
    category, mechanisms = classify(question)
    assert isinstance(category, str) and category
    assert len(mechanisms) >= 3
    assert len({item.statement for item in mechanisms}) == len(mechanisms)
    assert all(item.experiment for item in mechanisms)


# --------------------------------------------------------------------------- #
# Design capacity is arithmetic on stated input, or absent
# --------------------------------------------------------------------------- #


@given(question=QUESTIONS)
@QUIET
def test_no_quantity_is_extracted_that_is_not_in_the_text(question):
    extracted = extract_requirements(question)
    digits = set(re.findall(r"\d", question))
    if not digits:
        assert extracted == {}, f"invented {extracted} from text with no digits"


@given(
    volume=st.integers(min_value=1, max_value=10**9),
    payload=st.integers(min_value=1, max_value=4096),
)
@QUIET
def test_a_stated_volume_is_carried_through_unchanged(volume, payload):
    extracted = extract_requirements(f"{volume} requests per second at {payload}kb each")
    assert extracted["volume_per_second"] == float(volume)
    assert extracted["payload_kb"] == float(payload)


@pytest.mark.asyncio
@given(question=QUESTIONS)
@QUIET
async def test_a_design_states_its_arithmetic_or_states_nothing(question):
    report = await DesignWorkflow(CapabilityGuard()).run(PlatformRequest(question=question))
    body = "\n".join(report.facts)
    if "Capacity (calculated" in body:
        assert extract_requirements(question), "capacity appeared without a stated quantity"
    assert "estimated at" not in body.lower()
