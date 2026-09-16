"""Command-bar intent classification.

Deterministic pattern matching over the operator's input. It routes; it does not
decide anything, and it never calls a model.
"""

from __future__ import annotations

import re

from devlens.domain import IntentGuess

TICKET = re.compile(r"\b([A-Z][A-Z0-9_]*-[1-9][0-9]*)\b")
PR = re.compile(r"(?:pull[-/]?requests?|/pulls?|\bPR\b)[ /#]*(\d+)", re.I)
REPO = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")

RULES: tuple[tuple[str, str], ...] = (
    (r"\breview\b|pull request|\bPR\b|\bdiff\b", "CODE_REVIEW"),
    (r"p9[59]|latency|\bslow\b|timeout|throughput|\bperf", "PERFORMANCE"),
    (r"\bwhy\b|root cause|debug|investigate|failing|broken|error rate", "DEBUG"),
    (r"design|architecture|capacity|throughput plan|\bscale\b", "SYSTEM_DESIGN"),
    (r"observ|loki|prometheus|tempo|tracing|metrics|logging", "OBSERVABILITY"),
    (r"implement|\bfix\b|patch|build\b", "IMPLEMENTATION"),
)


def detect_intent(text: str, override: str | None = None) -> IntentGuess:
    raw = (text or "").strip()
    lowered = raw.lower()
    ticket = TICKET.search(raw)
    pull = PR.search(raw)
    repo = REPO.search(raw)

    if override:
        intent = override
    elif pull:
        intent = "CODE_REVIEW"
    else:
        intent = next(
            (name for pattern, name in RULES if re.search(pattern, lowered)),
            "JIRA_ANALYSIS" if ticket else "QUESTION",
        )
    return IntentGuess(
        intent=intent,
        ticket_key=ticket.group(1) if ticket else None,
        pr=int(pull.group(1)) if pull else None,
        repository=repo.group(1) if repo and "/" in repo.group(1) else None,
        question=raw or None,
    )
