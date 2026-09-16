"""Workflows: what DevLens actually produces, and what it refuses to produce.

The audit's central charge against the workflow layer was that output was
templated rather than derived — one hard-coded hypothesis, a design worksheet
with invented capacity numbers, a review that reported findings it had not
located. These tests assert derivation: change the input, the output changes;
withhold the input, the output says what is missing.

Regression coverage for DL-P1-001 (a single hard-coded hypothesis),
DL-P1-002 (invented capacity figures) and DL-P2-013 (review findings without a
file or line).
"""

from __future__ import annotations

import pytest

from devlens.agent.context_builder import eligible, shortlist, tokenize
from devlens.agent.intent import detect_intent
from devlens.agent.workflows.analyze import AnalyzeWorkflow
from devlens.agent.workflows.platform import (
    AskWorkflow,
    DesignWorkflow,
    InvestigateWorkflow,
    ObserveWorkflow,
    classify,
    extract_requirements,
)
from devlens.domain import AnalyzeRequest, PlatformRequest, PullRequestFile
from devlens.guardrails import CapabilityGuard
from devlens.runtime import RunContext
from devlens.tools.review_heuristics import missing_tests, parse_added_lines, review_changes

# --------------------------------------------------------------------------- #
# Investigate: hypotheses are derived from the question
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "marker"),
    [
        ("p99 latency tripled after the deploy", "latency"),
        ("the API returns 500s intermittently", "failure"),
        ("the ingest worker is OOM killed", "resource"),
        ("customers are charged twice on retry", "correctness"),
        ("users get 403 on their own records", "authorization"),
    ],
)
async def test_different_symptoms_produce_different_hypotheses(question, marker):
    """DL-P1-001: not one sentence with the question pasted into it."""
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question=question)
    )
    statements = [item.statement for item in report.hypotheses]
    assert len(set(statements)) == len(statements) >= 3
    assert classify(question)[0] == marker
    assert all(item.next_experiment for item in report.hypotheses)


@pytest.mark.asyncio
async def test_two_unrelated_questions_do_not_share_a_symptom_hypothesis():
    """Two generic mechanisms apply everywhere; the symptom-specific ones do not."""
    latency = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="p99 latency tripled")
    )
    charges = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="customers are charged twice")
    )
    generic = {
        mechanism.statement for mechanism in classify("something with no symptom")[1]
    }
    assert len(generic) == 3, "the shared tail is the cross-cutting mechanisms"

    def symptom_specific(report):
        return {item.statement for item in report.hypotheses} - generic

    assert "pool is saturated" in " ".join(symptom_specific(latency))
    assert "idempoten" in " ".join(symptom_specific(charges)).lower()
    assert not (symptom_specific(latency) & symptom_specific(charges))


@pytest.mark.asyncio
async def test_an_unclassifiable_question_says_so_rather_than_guessing():
    report = await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="something feels off")
    )
    assert report.root_cause is None
    assert report.unknowns


@pytest.mark.asyncio
async def test_a_run_context_receives_progress_while_the_work_happens():
    context = RunContext()
    await InvestigateWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="p99 latency tripled"), context=context
    )
    kinds = [event.kind for event in context.events]
    assert "HypothesisCreated" in kinds


# --------------------------------------------------------------------------- #
# Design: numbers are calculated or absent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("10k events per second, 2kb payload", {"volume_per_second": 10_000.0, "payload_kb": 2.0}),
        ("1 million requests per day", {"volume_per_day": 1_000_000.0}),
        ("2 MB payloads", {"payload_kb": 2048.0}),
        ("p99 under 200 ms", {"latency_target": "p99 < 200ms"}),
    ],
)
def test_only_stated_quantities_are_extracted(question, expected):
    """DL-P1-002: the worksheet may not invent a plausible number."""
    extracted = extract_requirements(question)
    for key, value in expected.items():
        assert extracted[key] == value


def test_a_question_with_no_numbers_yields_no_numbers():
    assert extract_requirements("design a notification system") == {}
    assert extract_requirements("") == {}


@pytest.mark.asyncio
async def test_a_design_without_stated_volume_names_the_missing_input():
    report = await DesignWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="design a notification system")
    )
    body = "\n".join(report.facts + report.unknowns)
    assert "volume" in body.lower() or "capacity" in body.lower()
    assert not any(char.isdigit() and "estimated" in body for char in body)


@pytest.mark.asyncio
async def test_a_design_with_stated_volume_shows_its_arithmetic():
    report = await DesignWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="20k writes per second at 1kb each")
    )
    body = "\n".join(report.facts)
    assert "calculated, not estimated by a model" in body
    assert "20000" in body.replace(",", "") or "20000.0" in body.replace(",", "")


@pytest.mark.asyncio
async def test_the_design_worksheet_covers_every_declared_section():
    report = await DesignWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="design a feed")
    )
    rendered = "\n".join(report.facts + [item.statement for item in report.hypotheses])
    for title, _ in DesignWorkflow.SECTIONS[:6]:
        assert title.lower() in rendered.lower() or title in str(report.model_dump())


# --------------------------------------------------------------------------- #
# Analyze and observe: real repositories only
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_analyze_reads_the_repository_it_was_given(git_repo):
    context = RunContext()
    report = await AnalyzeWorkflow().run(
        AnalyzeRequest(path=str(git_repo), query="instagram"), context=context
    )
    body = "\n".join(report.facts + report.observations)
    assert "app.py" in body or "instagram" in body
    assert {call.tool for call in context.calls} >= {"local.tree", "local.log"}
    assert all(call.status == "ok" for call in context.calls)


@pytest.mark.asyncio
async def test_analyze_refuses_a_directory_that_is_not_a_repository(tmp_path):
    from devlens.domain import ProviderError

    with pytest.raises(ProviderError, match="not a git repository"):
        await AnalyzeWorkflow().run(AnalyzeRequest(path=str(tmp_path)))


@pytest.mark.asyncio
async def test_observe_reports_what_is_missing_with_a_verification_step(git_repo):
    report = await ObserveWorkflow(CapabilityGuard()).run(
        PlatformRequest(path=str(git_repo))
    )
    assert report.findings
    assert all(item.verification for item in report.findings)
    assert all(item.category == "observability" for item in report.findings)


@pytest.mark.asyncio
async def test_ask_without_a_model_answers_deterministically_and_says_so():
    report = await AskWorkflow(CapabilityGuard()).run(
        PlatformRequest(question="what does idempotency mean here?")
    )
    assert any("deterministic" in item for item in report.limitations)


# --------------------------------------------------------------------------- #
# Review heuristics: a finding must point at something
# --------------------------------------------------------------------------- #


DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,7 @@
 import requests
+API_KEY = "AKIAIOSFODNN7EXAMPLE"
+def fetch(url):
+    return requests.get(url)
+
"""


def test_added_lines_are_parsed_with_their_numbers():
    added = parse_added_lines(DIFF)
    assert "app.py" in added
    numbers = [line for line, _ in added["app.py"]]
    assert numbers == sorted(numbers)
    assert any("API_KEY" in text for _, text in added["app.py"])


def test_every_finding_points_at_a_file_and_a_line():
    """DL-P2-013: a finding with no location cannot be verified or fixed."""
    findings = review_changes(DIFF, [PullRequestFile(path="app.py", status="modified")], {})
    assert findings
    for finding in findings:
        assert finding.file
        assert finding.line is None or finding.line > 0
        assert finding.evidence
        assert finding.verification or finding.remediation


def test_a_credential_in_a_diff_is_reported_and_masked():
    findings = review_changes(DIFF, [PullRequestFile(path="app.py", status="modified")], {})
    secret = [item for item in findings if "secret" in item.title.lower()
              or "credential" in item.title.lower()]
    assert secret, "a hard-coded AWS key must be reported"
    assert "AKIAIOSFODNN7EXAMPLE" not in str([item.model_dump() for item in secret])


def test_a_new_source_file_without_a_test_is_reported():
    findings = missing_tests([PullRequestFile(path="src/pay.py", status="added")])
    assert len(findings) == 1
    assert findings[0].file == "src/pay.py"
    assert findings[0].severity == "P3"
    assert "test" in findings[0].title.lower()


def test_a_new_file_that_brings_its_own_test_is_not_reported():
    assert missing_tests(
        [
            PullRequestFile(path="src/pay.py", status="added"),
            PullRequestFile(path="tests/test_pay.py", status="added"),
        ]
    ) == []


def test_modifying_an_existing_file_is_not_a_missing_test_finding():
    """Only new source files are reported; the rest would be noise."""
    assert missing_tests([PullRequestFile(path="src/pay.py", status="modified")]) == []


def test_an_empty_diff_produces_no_findings():
    assert review_changes("", [], {}) == []


# --------------------------------------------------------------------------- #
# Context selection and intent
# --------------------------------------------------------------------------- #


def test_keywords_come_from_the_ticket_not_from_a_fixed_list():
    terms = tokenize("Checkout times out on the instagram AccountResolver")
    assert {"checkout", "instagram", "account", "resolver"} <= terms
    assert "on" not in terms, "one- and two-letter tokens are noise"
    assert "please" not in tokenize("please fix this ticket"), "stop words are dropped"


@pytest.mark.parametrize(
    ("path", "size", "ok"),
    [
        ("src/app.py", 1000, True),
        ("node_modules/x/index.js", 1000, False),
        ("dist/bundle.js", 1000, False),
        ("image.png", 1000, False),
        ("src/huge.py", 50_000_000, False),
    ],
)
def test_only_plausible_source_files_are_eligible(path, size, ok):
    assert eligible(path, size) is ok


def test_files_are_ranked_by_relevance_to_the_ticket():
    from devlens.domain import RepositoryFile

    files = [
        RepositoryFile(path="src/checkout_service.py", size=100),
        RepositoryFile(path="src/unrelated.py", size=100),
    ]
    ranked = shortlist(files, {"checkout"})
    assert ranked[0].path == "src/checkout_service.py"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("review PR 42 in org/repo", "CODE_REVIEW"),
        ("DEV-1 is failing in production", "DEBUG"),
        ("design a rate limiter", "SYSTEM_DESIGN"),
        ("what is a circuit breaker?", "QUESTION"),
    ],
)
def test_intent_is_detected_from_the_text(text, intent):
    assert detect_intent(text).intent == intent


def test_an_explicit_override_wins_over_detection():
    assert detect_intent("anything at all", override="SYSTEM_DESIGN").intent == (
        "SYSTEM_DESIGN"
    )


def diff_for(path: str, *lines: str) -> str:
    """A unified diff whose added lines are exactly ``lines``."""
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,1 +1,{len(lines) + 1} @@\n context\n{body}"
    )


def diff_at(path: str, number: int, *lines: str) -> str:
    """A diff whose added lines land at ``number`` in the post-image.

    The heuristics only look at the lines a pull request actually changed, so a
    test that wants a hit inside a loop has to place the change inside it.
    """
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{number},0 +{number},{len(lines)} @@\n{body}"
    )

# --------------------------------------------------------------------------- #
# The n+1 heuristic, in both languages it understands
# --------------------------------------------------------------------------- #


PYTHON_LOOP = """def load(ids):
    out = []
    for identifier in ids:
        out.append(Model.objects.get(id=identifier))
    return out
"""

PYTHON_BATCHED = """def load(ids):
    rows = Model.objects.filter(id__in=ids)
    return [row for row in rows]
"""

RUBY_LOOP = """def load(ids)
  ids.each do |id|
    Order.find(id)
  end
end
"""


def test_a_query_in_a_changed_python_loop_is_reported_with_its_line():
    findings = review_changes(
        diff_at("app.py", 4, "        out.append(Model.objects.get(id=identifier))"),
        [PullRequestFile(path="app.py", status="modified")],
        {"app.py": PYTHON_LOOP},
    )
    n_plus_one = [item for item in findings if item.category == "performance"]
    assert n_plus_one, "a query inside a changed loop should be reported"
    assert n_plus_one[0].file == "app.py"
    assert n_plus_one[0].line
    assert n_plus_one[0].status == "HYPOTHESIS", "a heuristic is not a confirmation"
    assert "query count" in (n_plus_one[0].required_test or n_plus_one[0].verification or "")


def test_a_batched_query_is_not_reported():
    findings = review_changes(
        diff_for("app.py", "    rows = Model.objects.filter(id__in=ids)"),
        [PullRequestFile(path="app.py", status="modified")],
        {"app.py": PYTHON_BATCHED},
    )
    assert [item for item in findings if item.category == "performance"] == []


def test_a_loop_the_pull_request_did_not_touch_is_not_reported():
    """The heuristic is restricted to the lines this change actually altered."""
    findings = review_changes(
        diff_for("app.py", "    return out"),
        [PullRequestFile(path="app.py", status="modified")],
        {"app.py": PYTHON_LOOP},
    )
    assert [item for item in findings if item.category == "performance"] == []


def test_a_file_that_does_not_parse_is_skipped_rather_than_crashing():
    findings = review_changes(
        diff_for("app.py", "    Model.objects.get(id=1)"),
        [PullRequestFile(path="app.py", status="modified")],
        {"app.py": "def broken(:\n"},
    )
    assert isinstance(findings, list)


def test_the_heuristic_also_reads_a_ruby_loop():
    findings = review_changes(
        diff_at("app.rb", 3, "    Order.find(id)"),
        [PullRequestFile(path="app.rb", status="modified")],
        {"app.rb": RUBY_LOOP},
    )
    assert any(item.category == "performance" for item in findings)


def test_one_finding_per_loop_rather_than_one_per_line():
    content = PYTHON_LOOP.replace(
        "        out.append(Model.objects.get(id=identifier))",
        "        out.append(Model.objects.get(id=identifier))\n"
        "        out.append(Other.objects.get(id=identifier))",
    )
    findings = review_changes(
        diff_at(
            "app.py",
            4,
            "        out.append(Model.objects.get(id=identifier))",
            "        out.append(Other.objects.get(id=identifier))",
        ),
        [PullRequestFile(path="app.py", status="modified")],
        {"app.py": content},
    )
    assert len([item for item in findings if item.category == "performance"]) == 1
