"""Markdown rendering for reports.

Used both by ``GET /jobs/{id}/export`` and to build the body of a proposed Jira
or pull-request comment, so what an operator reads in the UI is what gets
posted if they approve it.
"""

from __future__ import annotations

from typing import Any

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def to_markdown(payload: dict) -> str:
    if _is_ticket_result(payload):
        return _ticket_markdown(payload)
    return _report_markdown(payload)


def _is_ticket_result(payload: dict) -> bool:
    return "ticket" in payload and "commit" in payload and "evidence" in payload


def _ticket_markdown(payload: dict) -> str:
    ticket = payload.get("ticket") or {}
    lines = [
        "# DevLens ticket analysis",
        "",
        f"## {ticket.get('key', '')} — {ticket.get('summary', '')}",
        "",
        payload.get("summary") or "",
        "",
        f"Repository `{payload.get('repository')}` at commit "
        f"`{payload.get('commit')}` (requested ref `{payload.get('requested_ref')}`).",
        f"DevLens {payload.get('devlens_version')}, workflow "
        f"{payload.get('workflow_version')}.",
        "",
        "## Evidence",
        "",
    ]
    for item in payload.get("evidence") or []:
        lines.append(_evidence_line(item))
    lines += _section("Not inspected", [
        _truncation_line(item) for item in payload.get("truncations") or []
    ])
    lines += _section("Limitations", payload.get("limitations") or [])
    return "\n".join(lines).strip() + "\n"


def _report_markdown(payload: dict) -> str:
    lines = [
        f"# DevLens — {payload.get('task', 'report')}",
        "",
        payload.get("executive_summary") or "",
        "",
    ]
    if payload.get("root_cause"):
        lines += [
            "## Root cause",
            "",
            f"**{payload['root_cause']}** "
            f"(`{payload.get('root_cause_status', 'UNKNOWN')}`)",
            "",
        ]
    hypotheses = payload.get("hypotheses") or []
    if hypotheses:
        lines += ["## Hypotheses", ""]
        for item in hypotheses:
            lines.append(
                f"- **{item.get('id')}** `{item.get('status')}` "
                f"/ confidence `{item.get('confidence')}` — {item.get('statement')}"
            )
            if item.get("next_experiment"):
                lines.append(f"  - Next: {item['next_experiment']}")
        lines.append("")
    findings = payload.get("findings") or []
    if findings:
        lines += ["## Findings", ""]
        for item in sorted(
            (f for f in findings if isinstance(f, dict)),
            key=lambda f: SEVERITY_ORDER.get(f.get("severity", "P3"), 3),
        ):
            location = (
                f" (`{item.get('file')}:{item.get('line') or '-'}`)"
                if item.get("file")
                else ""
            )
            lines.append(
                f"- **{item.get('severity')}** `{item.get('status')}` "
                f"/ `{item.get('confidence')}` — {item.get('title')}{location}"
            )
            for label, key in (
                ("Scenario", "production_scenario"),
                ("Impact", "impact"),
                ("Fix", "recommended_fix"),
                ("Verify", "verification"),
            ):
                if item.get(key):
                    lines.append(f"  - {label}: {item[key]}")
        lines.append("")
    evidence = payload.get("evidence_items") or []
    if evidence:
        lines += ["## Evidence", ""]
        lines.extend(_evidence_line(item) for item in evidence)
        lines.append("")
    providers = payload.get("provider_results") or []
    if providers:
        lines += ["## Provider availability", ""]
        for item in providers:
            detail = f" — {item.get('error')}" if item.get("error") else ""
            lines.append(f"- `{item.get('status')}` {item.get('provider')}{detail}")
        lines.append("")
    for heading, key in (
        ("Facts", "facts"),
        ("Observations", "observations"),
        ("Unknowns", "unknowns"),
        ("Implementation plan", "implementation_plan"),
        ("Verification", "verification_plan"),
        ("Risks", "risks"),
    ):
        lines += _section(heading, payload.get(key) or [])
    lines += _section(
        "Not inspected", [_truncation_line(item) for item in payload.get("truncations") or []]
    )
    lines += _section("Limitations", payload.get("limitations") or [])
    proposals = payload.get("proposals") or []
    if proposals:
        lines += ["## Proposed actions (require approval)", ""]
        for item in proposals:
            lines.append(f"- `{item.get('action')}` on {item.get('target')} — {item.get('rationale')}")
        lines.append("")
    if payload.get("diff"):
        lines += ["## Diff", "", "```diff", str(payload["diff"])[:20_000], "```", ""]
    return "\n".join(lines).strip() + "\n"


def _section(heading: str, values: list) -> list[str]:
    if not values:
        return []
    return [f"## {heading}", "", *(f"- {item}" for item in values), ""]


def _evidence_line(item: Any) -> str:
    if not isinstance(item, dict):
        return f"- {item}"
    provenance = item.get("provenance") or {}
    location = provenance.get("url") or provenance.get("source") or item.get("source")
    excerpt = f" — `{item.get('excerpt')}`" if item.get("excerpt") else ""
    return (
        f"- `{item.get('status', 'UNKNOWN')}` [{item.get('type')}] "
        f"{item.get('observation')} ({location}){excerpt}"
    )


def _truncation_line(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    total = f" of {item['total']}" if item.get("total") is not None else ""
    return f"{item.get('source')}: inspected {item.get('fetched')}{total} ({item.get('reason')})"
