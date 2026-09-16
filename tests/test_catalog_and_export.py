"""Catalog, persistence and report rendering.

These are the surfaces an operator reads rather than the ones that touch a
remote, and the audit's charge against them was that they invented content: a
capacity table with no stated inputs, a service catalog implying discovery, a
markdown export that dropped the caveats. What is asserted here is the reverse:
every number is arithmetic on a supplied input, the catalog says it is declared,
and the export keeps the truncations and limitations.

Regression coverage for DL-P2-027 (UI state was lost on restart), DL-P3-031
(exports dropped limitations) and DL-P3-034 (the catalog implied discovery).
"""

from __future__ import annotations

import time

import pytest

from devlens.app.catalog import (
    KnowledgeCreate,
    KnowledgeStore,
    OnboardingStore,
    capacity,
    notifications,
    search,
    services,
    settings_section,
)
from devlens.app.export import to_markdown
from devlens.app.persistence import (
    PersistentKnowledgeStore,
    PersistentOnboardingStore,
    SQLiteStateRepository,
)
from devlens.domain import ApprovalRecord, JobRecord


def job(**overrides) -> JobRecord:
    return JobRecord(
        **{
            "id": "job-1",
            "run_id": "run-1",
            "command": "ticket",
            "project": "default",
            "status": "completed",
            "request": {"ticket_key": "DEV-1"},
            "result": {"executive_summary": "checkout times out"},
            "created_at": time.time(),
            **overrides,
        }
    )


def approval(**overrides) -> ApprovalRecord:
    now = time.time()
    return ApprovalRecord(
        **{
            "id": "ap-1",
            "proposal_id": "p-1",
            "action": "post_pr_comment",
            "target": "org/repo#7",
            "payload_sha256": "a" * 64,
            "status": "pending",
            "created_at": now,
            "expires_at": now + 3600,
            **overrides,
        }
    )


# --------------------------------------------------------------------------- #
# Capacity: arithmetic only
# --------------------------------------------------------------------------- #


def test_capacity_is_arithmetic_on_the_supplied_inputs():
    result = capacity(events_per_day=86_400, peak_multiplier=10, payload_kb=40)
    assert result["average_per_sec"] == 1.0
    assert result["peak_per_sec"] == 10.0
    assert result["daily_ingress_gb"] == round(86_400 * 40 / 1024 / 1024, 3)
    assert "no model is involved" in result["method"]


def test_capacity_with_no_volume_produces_zeroes_not_a_plausible_guess():
    result = capacity(events_per_day=0)
    assert result["average_per_sec"] == 0.0
    assert result["peak_per_sec"] == 0.0


def test_a_negative_or_sub_unit_input_is_clamped_rather_than_propagated():
    result = capacity(events_per_day=-5, peak_multiplier=0.1, payload_kb=-1)
    assert result["events_per_day"] == 0.0
    assert result["peak_multiplier"] == 1.0
    assert result["payload_kb"] == 0.0


# --------------------------------------------------------------------------- #
# Services: declared, never discovered
# --------------------------------------------------------------------------- #


def test_the_catalog_is_built_from_declarations_and_the_allowlist():
    """DL-P3-034: nothing here is discovered, and the UI must not imply it is."""
    from devlens.app.config import ServiceEntry
    from devlens.domain import ProjectPublic

    items = services(
        [job(command="review", request={"repository": "org/repo"})],
        [
            ProjectPublic(
                name="default",
                repositories=["org/repo"],
                jira_projects=["DEV"],
                git_provider="github",
            )
        ],
        {"checkout": ServiceEntry(repository="org/repo", language="python")},
    )
    names = {item["name"] for item in items}
    assert "checkout" in names
    assert any(item.get("repository") == "org/repo" for item in items)
    declared = next(item for item in items if item["name"] == "checkout")
    assert declared["language"] == "python"


def test_an_empty_catalog_is_empty_rather_than_invented():
    assert services([], [], {}) == []


# --------------------------------------------------------------------------- #
# Notifications and search
# --------------------------------------------------------------------------- #


def test_notifications_surface_pending_approvals_and_finished_runs():
    items = notifications([job(), job(id="job-2", status="failed", error="boom")], [approval()])
    kinds = {item["kind"] for item in items}
    assert kinds == {"approval.requested", "run.completed", "run.failed"}
    assert all(item["href"] for item in items)


def test_notifications_are_bounded():
    many = [job(id=f"job-{index}") for index in range(50)]
    assert len(notifications(many, [])) == 20


def test_search_finds_a_run_by_its_ticket_and_by_its_summary():
    results = search("DEV-1", [job()], [], [], [], [])
    assert results["groups"]["runs"]
    assert results["groups"]["investigations"]
    by_summary = search("checkout", [job()], [], [], [], [])
    assert by_summary["groups"]["runs"]


def test_search_with_an_empty_query_returns_empty_groups():
    results = search("  ", [job()], [], [], [], [])
    assert all(items == [] for items in results["groups"].values())


def test_search_results_are_bounded_per_group():
    many = [job(id=f"job-{index}") for index in range(60)]
    assert len(search("ticket", many, [], [], [], [])["groups"]["runs"]) == 25


# --------------------------------------------------------------------------- #
# Knowledge and onboarding, with and without persistence
# --------------------------------------------------------------------------- #


def test_knowledge_documents_round_trip():
    store = KnowledgeStore()
    created = store.create(
        KnowledgeCreate(title="Runbook", category="runbook", body="steps")
    )
    assert store.get(created.id) == created
    assert store.list(category="runbook") == [created]
    assert store.list(category="other") == []
    assert store.get("absent") is None


def test_knowledge_survives_a_restart(tmp_path):
    """DL-P2-027: an in-memory store loses everything the operator wrote."""
    path = tmp_path / "state.sqlite"
    first = PersistentKnowledgeStore(SQLiteStateRepository(path))
    created = first.create(KnowledgeCreate(title="Runbook", category="runbook", body="s"))

    second = PersistentKnowledgeStore(SQLiteStateRepository(path))
    assert [item.id for item in second.list()] == [created.id]


def test_onboarding_progress_survives_a_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    first = PersistentOnboardingStore(SQLiteStateRepository(path))
    first.mark("configure")

    second = PersistentOnboardingStore(SQLiteStateRepository(path))
    view = second.view(has_settings=True, has_run=False)
    assert any(step.get("done") for step in view["steps"])


def test_onboarding_reflects_actual_state_not_a_stored_claim():
    store = OnboardingStore()
    nothing = store.view(has_settings=False, has_run=False)
    configured = store.view(has_settings=True, has_run=True)
    assert nothing != configured
    assert configured["steps"] != nothing["steps"]


def test_an_unknown_state_key_returns_the_default(tmp_path):
    repository = SQLiteStateRepository(tmp_path / "state.sqlite")
    assert repository.read("absent", default=[]) == []
    repository.write("present", {"a": 1})
    assert repository.read("present", default=None) == {"a": 1}


# --------------------------------------------------------------------------- #
# Settings sections
# --------------------------------------------------------------------------- #


def test_every_declared_settings_section_renders():
    from devlens.app.catalog import SETTINGS_SECTIONS

    public = {
        "capabilities": {"enabled": [], "denied": ["llm"], "reasons": {"llm": "off"},
                         "enable_with": {"llm": "DEVLENS_LLM_PROVIDER"}},
        "projects": [],
        "analyze_root": "/tmp",
        "auth_required": False,
        "weak_password": False,
        "session_seconds": 3600,
        "version": "0.2.0",
        "resilience": None,
        "model": None,
        "llm_provider": None,
        "llm_auth": {},
        "model_budget": None,
        "config_error": None,
    }
    for name in SETTINGS_SECTIONS:
        assert settings_section(name, public, [])["section"] == name


def test_an_unknown_settings_section_is_a_key_error():
    with pytest.raises(KeyError):
        settings_section("nope", {}, [])


# --------------------------------------------------------------------------- #
# Markdown export
# --------------------------------------------------------------------------- #


def test_a_ticket_export_keeps_the_caveats():
    """DL-P3-031: an export that drops the limitations overstates the analysis."""
    markdown = to_markdown(
        {
            "ticket": {"key": "DEV-1", "summary": "Checkout times out"},
            "commit": "a" * 40,
            "requested_ref": "HEAD",
            "repository": "org/repo",
            "devlens_version": "0.2.0",
            "workflow_version": "1",
            "summary": "Lexical change surface located.",
            "evidence": [
                {
                    "id": "E-1",
                    "type": "code",
                    "source": "src/checkout.py:12",
                    "observation": "no timeout on the client",
                    "status": "SUPPORTED",
                }
            ],
            "truncations": [
                {"source": "DEV-1 comments", "fetched": 30, "total": 900,
                 "reason": "capped at 300"}
            ],
            "limitations": ["Lexical relevance only."],
        }
    )
    assert "# DevLens ticket analysis" in markdown
    assert "DEV-1" in markdown
    assert "src/checkout.py:12" in markdown
    assert "Not inspected" in markdown
    assert "900" in markdown
    assert "Lexical relevance only." in markdown


def test_a_report_export_orders_findings_by_severity():
    markdown = to_markdown(
        {
            "task": "review org/repo#7",
            "executive_summary": "Three findings.",
            "findings": [
                {"id": "F-3", "severity": "P3", "title": "Style", "file": "a.py"},
                {"id": "F-0", "severity": "P0", "title": "Hard-coded credential",
                 "file": "b.py"},
                {"id": "F-1", "severity": "P1", "title": "Missing timeout", "file": "c.py"},
            ],
            "limitations": ["No language model is used."],
        }
    )
    assert markdown.index("Hard-coded credential") < markdown.index("Missing timeout")
    assert markdown.index("Missing timeout") < markdown.index("Style")
    assert "No language model is used." in markdown


def test_an_empty_report_renders_without_raising():
    assert to_markdown({}).strip()


def test_an_export_states_when_nothing_was_confirmed():
    markdown = to_markdown(
        {
            "task": "investigate checkout",
            "executive_summary": "Providers were unreachable.",
            "root_cause": None,
            "root_cause_status": "UNKNOWN",
            "unknowns": ["Loki is unreachable."],
        }
    )
    assert "Loki is unreachable." in markdown
    assert "Root cause" not in markdown or "UNKNOWN" in markdown


# --------------------------------------------------------------------------- #
# The full report export
# --------------------------------------------------------------------------- #


def full_report() -> dict:
    """Every section a report can carry, so the renderer is exercised whole."""
    return {
        "task": "investigate checkout latency",
        "executive_summary": "A pool is saturated.",
        "root_cause": "The checkout connection pool saturates at peak.",
        "root_cause_status": "CONFIRMED",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "A connection pool is saturated.",
                "status": "confirmed",
                "confidence": "VERY_HIGH",
                "next_experiment": "Plot pool utilisation against p99.",
            },
            {
                "id": "H2",
                "statement": "A downstream call has no timeout.",
                "status": "open",
                "confidence": "LOW",
            },
        ],
        "findings": [
            {
                "id": "F-1",
                "severity": "P1",
                "status": "SUPPORTED",
                "confidence": "HIGH",
                "title": "Client constructed without a timeout",
                "file": "src/checkout.py",
                "line": 42,
                "production_scenario": "One slow dependency consumes the caller's budget.",
                "impact": "Every checkout request queues behind it.",
                "recommended_fix": "Pass an explicit timeout.",
                "verification": "Assert the client carries a timeout in a unit test.",
            },
            {
                "id": "F-2",
                "severity": "P3",
                "status": "HYPOTHESIS",
                "confidence": "LOW",
                "title": "Bare except swallows the failure",
            },
        ],
        "evidence_items": [
            {
                "id": "E-1",
                "type": "metric",
                "source": "Prometheus",
                "observation": "p99 tracks pool saturation",
                "status": "CONFIRMED",
                "excerpt": "pool_in_use / pool_size > 0.95",
                "provenance": {"url": "https://metrics.internal/graph", "source": "Prometheus"},
            },
            "a bare string the renderer must survive",
        ],
        "provider_results": [
            {"provider": "Prometheus", "status": "AVAILABLE"},
            {"provider": "Loki", "status": "UNAVAILABLE", "error": "connection refused"},
        ],
        "facts": ["Queried Prometheus over one hour."],
        "observations": ["The pool is configured at 10."],
        "unknowns": ["Tempo is not configured."],
        "implementation_plan": ["Raise the pool size behind a flag."],
        "verification_plan": ["Re-run the same query after the change."],
        "risks": ["A larger pool moves the bottleneck to the database."],
        "truncations": [
            {"source": "DEV-1 comments", "fetched": 30, "total": 900, "reason": "capped"},
            {"source": "files", "fetched": 30, "total": None, "reason": "page limit"},
            "a bare string truncation",
        ],
        "limitations": ["Lexical relevance only."],
        "proposals": [
            {
                "action": "post_pr_comment",
                "target": "org/repo#7",
                "rationale": "Publish the finding.",
            }
        ],
        "diff": "diff --git a/a.py b/a.py\n+timeout=5\n",
    }


def test_a_full_report_renders_every_section_it_carries():
    markdown = to_markdown(full_report())
    for heading in (
        "## Root cause",
        "## Hypotheses",
        "## Findings",
        "## Evidence",
        "## Provider availability",
        "## Facts",
        "## Observations",
        "## Unknowns",
        "## Implementation plan",
        "## Verification",
        "## Risks",
        "## Not inspected",
        "## Limitations",
        "## Proposed actions (require approval)",
        "## Diff",
    ):
        assert heading in markdown, f"{heading} is missing from the export"


def test_the_export_keeps_each_scale_on_its_own_terms():
    markdown = to_markdown(full_report())
    assert "`CONFIRMED`" in markdown
    assert "confidence `VERY_HIGH`" in markdown
    assert "**P1**" in markdown or "P1" in markdown
    # An unreachable provider is named as unreachable, with its reason.
    assert "`UNAVAILABLE` Loki — connection refused" in markdown


def test_the_export_says_what_a_proposal_would_do_and_that_it_needs_approval():
    markdown = to_markdown(full_report())
    assert "require approval" in markdown
    assert "post_pr_comment` on org/repo#7" in markdown


def test_the_export_survives_malformed_entries():
    markdown = to_markdown(full_report())
    assert "a bare string the renderer must survive" in markdown
    assert "a bare string truncation" in markdown


def test_a_truncation_without_a_total_still_reads_correctly():
    markdown = to_markdown(full_report())
    assert "inspected 30 of 900 (capped)" in markdown
    assert "inspected 30 (page limit)" in markdown


def test_an_oversized_diff_is_capped_in_the_export():
    payload = full_report()
    payload["diff"] = "x" * 50_000
    markdown = to_markdown(payload)
    assert len(markdown) < 40_000


def test_a_report_with_no_optional_section_renders_only_what_it_has():
    markdown = to_markdown({"task": "ask", "executive_summary": "Nothing is confirmed."})
    assert "Nothing is confirmed." in markdown
    for heading in ("## Root cause", "## Hypotheses", "## Findings", "## Diff"):
        assert heading not in markdown
