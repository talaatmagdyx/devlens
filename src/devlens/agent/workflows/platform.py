"""Ask, Investigate, Design and Observe.

These used to be the weakest surfaces: Investigate emitted a single hard-coded
hypothesis and Design emitted the same twenty-four "UNKNOWN" lines whatever you
asked it. Both are now derived from evidence.

Investigate classifies the symptom, proposes the mechanisms that actually cause
that class of symptom, links each to the code and telemetry evidence that was
retrieved, and lets :func:`devlens.evidence.settle` decide what is supported.
A hypothesis becomes *confirmed* only when runtime evidence confirms it, so
neither a template nor a model can promote one.
"""

from __future__ import annotations

import re

from devlens.agent.context_builder import tokenize
from devlens.domain import (
    AccessDenied,
    EngineeringReport,
    EvidenceItem,
    EvidenceStatus,
    Finding,
    Hypothesis,
    PlatformRequest,
    ProviderError,
    ProviderResult,
    ReportStatus,
)
from devlens.evidence import finding_band, from_provider, observed, retrieved, settle
from devlens.guardrails import CapabilityGuard
from devlens.providers.git.local import RepoWorkspace
from devlens.providers.llm import build_prompt
from devlens.runtime import RunContext
from devlens.sandbox.workspace import open_local

RUNTIME_SOURCES = (
    ("log", "Loki", "Logs"),
    ("metric", "Prometheus", "Metrics"),
    ("trace", "Tempo", "Traces"),
    ("sql", "SQL gateway", "Database"),
)

BOUNDARY_PATTERNS = {
    "HTTP": r"FastAPI|flask|express|ActionController|@app\.(get|post)|Rails::Engine|gin\.|net/http",
    "Database": r"ActiveRecord|sqlalchemy|SELECT\s|prisma|django\.db|pgx\.|database/sql",
    "Cache": r"\bredis\b|Redis|memcached",
    "Queue": r"bunny|pika|RabbitMQ|amqp|kafka|sqs|celery|faststream",
    "Object store": r"boto3|aws-sdk|S3Client|gcs|blob_service",
}

TELEMETRY_PATTERNS = {
    "structured logging": r"structlog|logrus|zap\.|slf4j|logging\.getLogger|winston",
    "metrics": r"prometheus_client|micrometer|statsd|opentelemetry\.metrics|expvar",
    "tracing": r"opentelemetry|jaeger|ddtrace|zipkin|otel",
    "correlation ids": r"correlation[_-]?id|request[_-]?id|trace[_-]?id|x-request-id",
}


# --------------------------------------------------------------------------- #
# Symptom taxonomy
# --------------------------------------------------------------------------- #


class Mechanism:
    """A candidate cause, and the experiment that would settle it."""

    def __init__(self, statement: str, experiment: str, markers: tuple[str, ...]):
        self.statement = statement
        self.experiment = experiment
        self.markers = markers


SYMPTOMS: tuple[tuple[str, str, tuple[Mechanism, ...]], ...] = (
    (
        r"timeout|timed out|slow|latency|p9[59]|hang|stall",
        "latency",
        (
            Mechanism(
                "A downstream call has no timeout, or retries without a ceiling, so "
                "one slow dependency consumes the caller's budget.",
                "Search the code path for a client constructed without a timeout, "
                "then compare p99 latency of the caller and the dependency over the "
                "same window.",
                ("timeout", "retry", "retries", "backoff", "client", "session"),
            ),
            Mechanism(
                "A connection or worker pool is saturated, so requests queue before "
                "any work starts.",
                "Plot pool utilisation and queue depth against request latency for "
                "the same window.",
                ("pool", "connection", "worker", "semaphore", "concurrency", "queue"),
            ),
            Mechanism(
                "A query issued inside a loop scales with row count, so latency "
                "tracks data growth rather than traffic.",
                "Count queries per request in a trace, then compare against row "
                "counts for the affected tenant.",
                ("select", "query", "find", "loop", "each", "batch"),
            ),
        ),
    ),
    (
        r"\b5\d\d\b|error rate|exception|crash|fail(ing|ure|ed)?|500",
        "failure",
        (
            Mechanism(
                "An unhandled error in a newly changed path escapes to the caller.",
                "Group errors by exception type and deploy time; correlate the "
                "first occurrence with a release.",
                ("except", "raise", "rescue", "panic", "throw", "error"),
            ),
            Mechanism(
                "A dependency is returning errors and the failure is being passed "
                "through rather than degraded.",
                "Compare the caller's error rate with each dependency's error rate "
                "over the same window.",
                ("client", "request", "http", "status", "response"),
            ),
            Mechanism(
                "Input validation accepts a shape the downstream code cannot "
                "handle.",
                "Sample failing requests and replay their payloads against the "
                "validation layer.",
                ("validate", "schema", "parse", "serializer", "pydantic"),
            ),
        ),
    ),
    (
        r"memory|leak|oom|out of memory|rss|heap",
        "resource",
        (
            Mechanism(
                "An unbounded cache or accumulator grows for the lifetime of the "
                "process.",
                "Plot RSS against process uptime and correlate with cache size "
                "metrics.",
                ("cache", "dict", "list", "append", "global", "store"),
            ),
            Mechanism(
                "A response or file is read fully into memory instead of streamed.",
                "Compare peak RSS with the size distribution of inbound payloads.",
                ("read", "content", "body", "load", "bytes"),
            ),
        ),
    ),
    (
        r"duplicate|twice|double|idempoten|replay",
        "correctness",
        (
            Mechanism(
                "A retry re-executes a non-idempotent operation because no "
                "idempotency key is carried.",
                "Search for a write path reachable from a retried call and check "
                "whether the request carries a deduplication key.",
                ("retry", "idempoten", "key", "insert", "publish", "send"),
            ),
            Mechanism(
                "Two workers process the same item because the claim is not atomic.",
                "Check whether the claim is a conditional update and count "
                "concurrent consumers.",
                ("lock", "claim", "update", "status", "worker", "consumer"),
            ),
        ),
    ),
    (
        r"permission|denied|unauthori[sz]ed|403|401|access",
        "authorization",
        (
            Mechanism(
                "A credential lacks a scope the code path requires.",
                "Compare the token's scopes with the scopes the failing endpoint "
                "documents.",
                ("token", "scope", "auth", "permission", "role", "policy"),
            ),
            Mechanism(
                "An allowlist or policy check rejects a value that should be "
                "accepted.",
                "Log the exact value and the allowlist at the point of refusal.",
                ("allow", "policy", "authorize", "deny", "acl"),
            ),
        ),
    ),
    (
        r"missing|lost|disappear|not saved|no record|gone|inconsistent|stale|mismatch",
        "data-integrity",
        (
            Mechanism(
                "A write and its side effect are not in one transaction, so a "
                "failure between them leaves the two out of step.",
                "Find the write and the publish, and check whether they share a "
                "transaction or an outbox; then count rows with no matching event.",
                ("commit", "transaction", "publish", "outbox", "session", "flush"),
            ),
            Mechanism(
                "A read is served from a replica or cache that lags the write.",
                "Compare replica lag and cache TTL against the window in which the "
                "record appears missing.",
                ("replica", "cache", "ttl", "read", "follower", "invalidate"),
            ),
            Mechanism(
                "A filter or tenant scope excludes the record rather than deleting "
                "it, so it is present but invisible.",
                "Re-run the query without the scope and compare the row counts.",
                ("filter", "where", "scope", "tenant", "soft_delete", "archived"),
            ),
        ),
    ),
    (
        r"deploy|rollout|release|restart|boot|startup|migration|rollback",
        "deployment",
        (
            Mechanism(
                "A migration and the code that depends on it were released in the "
                "wrong order, so one version runs against the other's schema.",
                "Compare the migration's deploy time with the rollout window, and "
                "check whether the change is backward compatible in both "
                "directions.",
                ("migration", "alter", "column", "schema", "deploy", "version"),
            ),
            Mechanism(
                "Startup depends on something that is not ready yet, so instances "
                "fail their first health check and restart.",
                "Read the first 60 seconds of an instance's logs and check what it "
                "waits for before serving.",
                ("startup", "health", "ready", "connect", "bootstrap", "probe"),
            ),
        ),
    ),
    (
        r"cost|spend|bill|expensive|budget|throttl|rate limit|quota",
        "cost",
        (
            Mechanism(
                "A call that used to be rare is now on a hot path, so volume "
                "rather than unit price moved the bill.",
                "Compare call counts per unit of traffic before and after the "
                "change, not the total.",
                ("client", "request", "call", "loop", "each", "fetch"),
            ),
            Mechanism(
                "Work is retried without a ceiling, so a failure multiplies its "
                "own cost.",
                "Count attempts per logical operation and compare with the retry "
                "policy's stated ceiling.",
                ("retry", "backoff", "attempt", "requeue", "redeliver"),
            ),
        ),
    ),
)

GENERIC = (
    Mechanism(
        "A recent change to this code path altered behaviour in a way the tests "
        "do not cover.",
        "Diff the path's history against the first report of the symptom.",
        ("commit", "change", "update", "refactor"),
    ),
    Mechanism(
        "Configuration differs between the environment that works and the one "
        "that does not.",
        "Diff the effective configuration of both environments.",
        ("config", "env", "setting", "flag", "toggle"),
    ),
    Mechanism(
        "A dependency moved: a library, an image or an upstream API changed "
        "under a version range that was never pinned.",
        "Compare the resolved dependency versions of a working build with the "
        "failing one.",
        ("requirements", "lock", "version", "image", "upgrade", "bump"),
    ),
)


def classify(question: str) -> tuple[str, tuple[Mechanism, ...]]:
    text = (question or "").lower()
    for pattern, category, mechanisms in SYMPTOMS:
        if re.search(pattern, text):
            return category, mechanisms + GENERIC
    return "unclassified", GENERIC


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


async def collect_runtime(
    guard: CapabilityGuard, query: str, run_id: str | None
) -> tuple[list[EvidenceItem], list[ProviderResult]]:
    """Query telemetry, recording availability honestly.

    A provider that is unreachable yields UNKNOWN evidence and a
    ``UNAVAILABLE`` result. Nothing here can become support for a hypothesis.
    """
    if not guard.has("observability_provider") or guard.observe is None:
        return (
            [
                observed(
                    kind,  # type: ignore[arg-type]
                    provider,
                    f"{label} UNKNOWN: {provider} is not configured. Runtime "
                    "verification cannot run.",
                    provider=provider,
                    provider_status="NOT_CONFIGURED",
                    run_id=run_id,
                )
                for kind, provider, label in RUNTIME_SOURCES
            ],
            [],
        )
    collected = await guard.collect_runtime(query)
    items, results = [], []
    for kind, _provider, label in RUNTIME_SOURCES:
        result = collected.get(kind)
        if result is None:  # pragma: no cover - collect() always returns all four
            continue
        results.append(result)
        items.append(from_provider(result, kind, label, run_id=run_id))  # type: ignore[arg-type]
    return items, results


async def local_evidence(
    path: str | None, query: str | None, run_id: str | None
) -> tuple[list[str], list[EvidenceItem], dict[str, str]]:
    if not path:
        return [], [], {}
    workspace = open_local(path)
    repo = RepoWorkspace(workspace)
    facts = [f"Workspace {workspace.root}."]
    items: list[EvidenceItem] = []
    hits_by_file: dict[str, str] = {}
    if query:
        hits = await repo.search(query)
        facts.append(f"Search {query!r}: {hits[:1500] or 'No matches.'}")
        for line in hits.splitlines()[:40]:
            file_part, _, rest = line.partition(":")
            number, _, text = rest.partition(":")
            if not number.isdigit():
                continue
            hits_by_file.setdefault(file_part, "")
            hits_by_file[file_part] += text + "\n"
        for file_part, excerpt in list(hits_by_file.items())[:10]:
            items.append(
                retrieved(
                    "code",
                    file_part,
                    f"{file_part} contains lines matching {query!r}.",
                    excerpt=excerpt[:800],
                    selectors=[query],
                    run_id=run_id,
                    path=file_part,
                )
            )
    return facts, items, hits_by_file


def summarise_counts(items: list[EvidenceItem]) -> str:
    supported = sum(1 for item in items if item.status == "SUPPORTED")
    unknown = sum(1 for item in items if item.status == "UNKNOWN")
    confirmed = sum(1 for item in items if item.status == "CONFIRMED")
    return (
        f"{confirmed} confirmed, {supported} supporting and {unknown} unknown "
        "evidence items"
    )


# --------------------------------------------------------------------------- #
# Ask
# --------------------------------------------------------------------------- #


class AskWorkflow:
    def __init__(self, guard: CapabilityGuard | None = None, tools=None):
        self.guard = guard or CapabilityGuard()
        self.tools = tools

    async def run(
        self, request: PlatformRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        run_id = context.run_id if context else None
        question = request.question or request.path or "engineering question"
        facts, items, _ = await local_evidence(request.path, request.question, run_id)
        if context:
            context.observation(f"Collected {len(items)} local evidence items")
        runtime_items, results = await collect_runtime(self.guard, question, run_id)
        items.extend(runtime_items)

        deterministic = (
            f"Answered from {summarise_counts(items)}. "
            + (
                "Runtime telemetry was not consulted."
                if not results
                else "Runtime telemetry was queried; see evidence for availability."
            )
        )
        summary, unknowns = deterministic, [
            "No language model is used; this is lexical and telemetry evidence only."
        ]
        if self.guard.has("llm"):
            sources = [
                (item.source, item.excerpt or item.observation)
                for item in items
                if item.status != "UNKNOWN"
            ][:8]
            try:
                summary = await self.guard.complete(build_prompt(question, sources))
                unknowns = [
                    "The summary is model-generated from the evidence above; the "
                    "evidence, not the prose, is the record."
                ]
                if context:
                    context.observation("Synthesised an answer from retrieved evidence")
            except (ProviderError, AccessDenied) as exc:
                summary = deterministic
                unknowns = [f"The model was unavailable ({exc}); this is the deterministic summary."]
        if context:
            context.completed()
        return EngineeringReport(
            run_id=run_id,
            task=f"ask {question[:80]}",
            status="completed" if (facts or request.question) else "insufficient_context",
            executive_summary=summary,
            investigated=["question"] + (["local repository"] if request.path else []),
            facts=facts or ["No repository path was provided."],
            observations=[request.question] if request.question else [],
            evidence_items=items,
            provider_results=results,
            timeline=context.events if context else [],
            tool_calls=context.calls if context else [],
            intent="QUESTION",
            stage="CONCLUDE",
            unknowns=unknowns,
            limitations=self.guard.limitations(),
        )


# --------------------------------------------------------------------------- #
# Investigate
# --------------------------------------------------------------------------- #


class InvestigateWorkflow:
    def __init__(self, guard: CapabilityGuard | None = None, tools=None):
        self.guard = guard or CapabilityGuard()
        self.tools = tools

    async def run(
        self, request: PlatformRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        run_id = context.run_id if context else None
        question = request.question or request.ticket_key or "unspecified symptom"
        category, mechanisms = classify(question)
        if context:
            context.event("SymptomClassified", f"symptom class: {category}")

        facts: list[str] = []
        items: list[EvidenceItem] = []
        try:
            facts, items, hits = await local_evidence(request.path, question, run_id)
        except ProviderError as exc:
            hits = {}
            if request.path:
                facts = [f"{request.path} is not a git workspace ({exc})."]

        ticket = None
        if request.ticket_key and self.tools is not None:
            try:
                ticket = await self.tools.issue_context(request, context=context)
                facts.append(f"Jira {ticket.key}: {ticket.summary}")
                items.append(
                    retrieved(
                        "jira",
                        ticket.key,
                        f"{ticket.key} reports: {ticket.spec.problem[:300]}",
                        excerpt=ticket.description[:800],
                        run_id=run_id,
                        url=ticket.url,
                    )
                )
                for item in ticket.attachments:
                    if item.injection_suspected:
                        items.append(
                            observed(
                                "document",
                                item.filename,
                                "Attachment contains instruction-like text and is "
                                "treated as data only.",
                                run_id=run_id,
                            )
                        )
            except (AccessDenied, ProviderError) as exc:
                items.append(
                    observed(
                        "jira",
                        request.ticket_key,
                        f"Jira context could not be read: {exc}",
                        run_id=run_id,
                    )
                )
        elif request.ticket_key:
            items.append(
                observed(
                    "jira",
                    request.ticket_key,
                    "Ticket key supplied, but no Jira credentials are configured "
                    "for this project, so the ticket was not read.",
                    run_id=run_id,
                )
            )

        runtime_items, results = await collect_runtime(self.guard, question, run_id)
        items.extend(runtime_items)

        if self.guard.has("screenshot_analysis") and request.path:
            items.extend(await self._screenshot(request.path, run_id, context))

        hypotheses = self._hypotheses(mechanisms, items, hits, question)
        if context:
            for item in hypotheses:
                context.hypothesis(f"{item.id}: {item.statement[:120]}")

        confirmed = [item for item in hypotheses if item.status == "confirmed"]
        supported = [item for item in hypotheses if item.status == "supported"]
        root_cause = confirmed[0].statement if confirmed else None
        findings = self._findings(hypotheses, items)
        reached = [result for result in results if result.reached]
        unreachable = [result for result in results if not result.reached and result.status != "NOT_CONFIGURED"]

        status: ReportStatus = (
            "completed" if confirmed else ("partial" if items else "insufficient_context")
        )
        if context:
            context.completed()
        return EngineeringReport(
            run_id=run_id,
            task=f"investigate {question[:80]}",
            status=status,
            executive_summary=self._summary(
                category, hypotheses, confirmed, supported, reached, unreachable
            ),
            investigated=["symptom classification", "local code"]
            + (["jira issue"] if ticket else [])
            + (["runtime telemetry"] if results else []),
            facts=facts,
            evidence_items=items,
            provider_results=results,
            hypotheses=hypotheses,
            findings=findings,
            root_cause=root_cause,
            root_cause_status="CONFIRMED" if confirmed else "UNKNOWN",
            timeline=context.events if context else [],
            tool_calls=context.calls if context else [],
            ticket=ticket,
            intent="DEBUG",
            stage="RECOMMEND" if supported or confirmed else "DISCOVER",
            unknowns=self._unknowns(hypotheses, results),
            recommended_fix=(
                f"Run the next experiment for {supported[0].id}: {supported[0].next_experiment}"
                if supported and not confirmed
                else None
            ),
            limitations=self.guard.limitations(),
        )

    async def _screenshot(
        self, path: str, run_id: str | None, context: RunContext | None
    ) -> list[EvidenceItem]:
        from pathlib import Path

        from devlens.app.jobs import authorize_analyze_path

        target = Path(path)
        if target.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            return []
        try:
            resolved = authorize_analyze_path(path)
            image = resolved.read_bytes()
        except (AccessDenied, OSError) as exc:
            return [observed("image", path, f"Screenshot could not be read: {exc}", run_id=run_id)]
        try:
            notes = await self.guard.analyze_screenshot(
                {"content": image, "mime_type": "image/png"}
            )
        except (ProviderError, AccessDenied) as exc:
            return [observed("image", path, f"Screenshot analysis failed: {exc}", run_id=run_id)]
        if context:
            context.observation("Described a screenshot; observations only")
        return [
            retrieved(
                "image",
                path,
                notes[0] if notes else "Screenshot described.",
                status="SUPPORTED",
                excerpt="\n".join(notes[:6]),
                run_id=run_id,
                path=path,
            )
        ]

    def _hypotheses(
        self,
        mechanisms: tuple[Mechanism, ...],
        items: list[EvidenceItem],
        hits: dict[str, str],
        question: str,
    ) -> list[Hypothesis]:
        """Link each candidate mechanism to the evidence that bears on it."""
        keywords = tokenize(question)
        hypotheses = []
        for index, mechanism in enumerate(mechanisms[:5], start=1):
            markers = set(mechanism.markers)
            supporting = [
                item.id
                for item in items
                if item.status in {"SUPPORTED", "CONFIRMED"}
                and (
                    markers & tokenize(f"{item.source} {item.excerpt or ''} {item.observation}")
                    or (item.type in {"log", "metric", "trace"} and markers & keywords)
                )
            ]
            candidate = Hypothesis(
                id=f"H{index}",
                statement=mechanism.statement,
                supporting_evidence=supporting,
                next_experiment=mechanism.experiment,
            )
            hypotheses.append(settle(candidate, items))
        ranked = sorted(
            hypotheses,
            key=lambda item: (
                {"confirmed": 0, "supported": 1, "open": 2, "rejected": 3}[item.status],
                -len(item.supporting_evidence),
            ),
        )
        return ranked

    def _findings(
        self, hypotheses: list[Hypothesis], items: list[EvidenceItem]
    ) -> list[Finding]:
        index = {item.id: item for item in items}
        findings = []
        for hypothesis in hypotheses:
            if hypothesis.status not in {"supported", "confirmed"}:
                continue
            files = sorted(
                {
                    path
                    for ref in hypothesis.supporting_evidence
                    if ref in index and (path := index[ref].provenance.path)
                }
            )
            status: EvidenceStatus = (
                "CONFIRMED" if hypothesis.status == "confirmed" else "SUPPORTED"
            )
            findings.append(
                Finding(
                    id=f"INV-{hypothesis.id}",
                    severity="P1" if hypothesis.status == "confirmed" else "P2",
                    status=status,
                    confidence=finding_band(status, len(hypothesis.supporting_evidence)),
                    category="investigation",
                    title=hypothesis.statement[:300],
                    file=files[0] if files else None,
                    production_scenario=hypothesis.next_experiment,
                    evidence=hypothesis.supporting_evidence,
                    impact="Matches the reported symptom class.",
                    recommended_fix=hypothesis.next_experiment,
                    verification=hypothesis.next_experiment,
                )
            )
        return findings

    def _summary(self, category, hypotheses, confirmed, supported, reached, unreachable) -> str:
        parts = [
            f"Classified the symptom as {category} and evaluated "
            f"{len(hypotheses)} candidate mechanisms."
        ]
        if confirmed:
            parts.append(f"{len(confirmed)} confirmed by runtime evidence.")
        elif supported:
            parts.append(
                f"{len(supported)} supported by code evidence; none confirmed, "
                "because code shows intent and not behaviour."
            )
        else:
            parts.append("None are supported by the evidence collected so far.")
        if unreachable:
            names = ", ".join(sorted({result.provider for result in unreachable}))
            parts.append(
                f"{names} could not be reached, so this investigation is "
                "incomplete rather than negative."
            )
        elif not reached:
            parts.append("No runtime telemetry is configured, so nothing is confirmed.")
        return " ".join(parts)

    def _unknowns(self, hypotheses, results) -> list[str]:
        unknowns = [
            f"{item.id} is unresolved. Next: {item.next_experiment}"
            for item in hypotheses
            if item.status in {"open", "supported"} and item.next_experiment
        ]
        for result in results:
            if result.status == "NOT_CONFIGURED":
                unknowns.append(f"{result.provider} is not configured.")
            elif not result.reached:
                unknowns.append(f"{result.provider} was unreachable: {result.error}")
            elif result.status == "EMPTY":
                unknowns.append(
                    f"{result.provider} returned no data for this window; that is "
                    "not evidence the behaviour is absent."
                )
        if not results:
            unknowns.append(
                "Logs, metrics, traces and database access are not configured, so "
                "no hypothesis can be confirmed."
            )
        return unknowns[:20]


# --------------------------------------------------------------------------- #
# Design
# --------------------------------------------------------------------------- #

QUANTITY = re.compile(
    # The scale alternatives are longest-first and word-bounded: with "m"
    # listed before "million", "1 million requests" matched the bare "m" and
    # then failed to find the noun, so the stated volume was silently dropped.
    r"(\d+(?:[.,]\d+)?)\s*(thousand|million|billion|bn|k|m|b)?\b\s*"
    r"(requests?|events?|messages?|writes?|reads?|rows?|users?|rps|qps)?",
    re.I,
)
SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
PER_DAY = re.compile(r"per\s*day|/\s*day|daily|a\s*day", re.I)
PER_SECOND = re.compile(r"per\s*second|/\s*s\b|rps|qps", re.I)
PAYLOAD = re.compile(r"(\d+(?:\.\d+)?)\s*(kb|mb|kib|mib)\b", re.I)
LATENCY = re.compile(r"p(50|75|90|95|99)\s*(?:of|<|under|below)?\s*(\d+)\s*(ms|s)\b", re.I)


def extract_requirements(question: str) -> dict[str, float | str]:
    """Pull the numbers an engineer actually stated out of the prompt.

    Only stated quantities are used. Nothing is assumed, and the worksheet says
    which inputs are missing rather than inventing a plausible number.
    """
    found: dict[str, float | str] = {}
    for match in QUANTITY.finditer(question or ""):
        raw, scale, noun = match.groups()
        if not noun:
            continue
        value = float(raw.replace(",", "")) * SCALE.get((scale or "").lower(), 1)
        window = "second" if PER_SECOND.search(question) else "day"
        found.setdefault(f"volume_per_{window}", value)
    payload = PAYLOAD.search(question or "")
    if payload:
        size = float(payload.group(1))
        found["payload_kb"] = size * (1024 if payload.group(2).lower() in {"mb", "mib"} else 1)
    latency = LATENCY.search(question or "")
    if latency:
        found["latency_target"] = f"p{latency.group(1)} < {latency.group(2)}{latency.group(3)}"
    return found


class DesignWorkflow:
    """A worksheet where each section is derived, or states what it needs."""

    SECTIONS = (
        ("Requirements", "the functional requirement in one sentence"),
        ("Assumptions", "the assumptions you are willing to make explicit"),
        ("Capacity estimation", "traffic volume, payload size and peak multiplier"),
        ("Architecture", "the components and their responsibilities"),
        ("APIs", "the externally visible operations"),
        ("Events", "what is published and who consumes it"),
        ("Data model", "the entities and their keys"),
        ("Consistency model", "what must be strongly consistent"),
        ("Delivery semantics", "at-most-once, at-least-once or exactly-once"),
        ("Idempotency", "the deduplication key for each write"),
        ("Backpressure", "what happens when the consumer is slower than the producer"),
        ("Retries", "the retry budget and backoff"),
        ("Dead letters", "where unprocessable work goes"),
        ("Failure scenarios", "the failures you will design for"),
        ("Scaling", "the dimension that scales first"),
        ("Observability", "the signals that would detect each failure"),
        ("Security", "the trust boundaries and what crosses them"),
        ("Deployment", "the rollout and rollback plan"),
        ("Cost", "the dominant cost driver"),
        ("Tradeoffs", "what you are deliberately giving up"),
        ("Rejected alternatives", "what you considered and why not"),
        ("Migration plan", "how you get there from the current system"),
        ("Testing strategy", "how each guarantee is verified"),
    )

    def __init__(self, guard: CapabilityGuard | None = None, tools=None):
        self.guard = guard or CapabilityGuard()
        self.tools = tools

    async def run(
        self, request: PlatformRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        run_id = context.run_id if context else None
        question = request.question or "system design"
        stated = extract_requirements(question)
        capacity = self._capacity(stated)
        items: list[EvidenceItem] = []
        facts: list[str] = []

        if question:
            facts.append(f"Requirements (stated): {question}")
            items.append(
                retrieved(
                    "document",
                    "stated requirements",
                    "Requirements as supplied by the operator.",
                    excerpt=question[:800],
                    run_id=run_id,
                )
            )
        if stated:
            facts.append(
                "Extracted quantities: "
                + ", ".join(f"{key}={value}" for key, value in sorted(stated.items()))
            )
        if capacity:
            facts.append(
                "Capacity (calculated, not estimated by a model): "
                + ", ".join(f"{key}={value}" for key, value in capacity.items())
            )
            items.append(
                retrieved(
                    "benchmark",
                    "capacity calculation",
                    "Derived arithmetically from the stated volume and payload.",
                    excerpt=str(capacity),
                    run_id=run_id,
                )
            )

        detected: dict[str, list[str]] = {}
        if request.path:
            detected = await self._scan(request.path)
            if detected:
                facts.append(
                    "Existing boundaries in the referenced workspace: "
                    + ", ".join(sorted(detected))
                )
                items.extend(
                    retrieved(
                        "code",
                        files[0],
                        f"{name} boundary already exists in the codebase.",
                        run_id=run_id,
                        path=files[0],
                    )
                    for name, files in detected.items()
                )

        unresolved = []
        for name, needs in self.SECTIONS:
            value = self._section(name, stated, capacity, detected)
            if value:
                facts.append(f"{name}: {value}")
            else:
                facts.append(f"{name}: UNKNOWN — supply {needs}.")
                unresolved.append(f"{name}: supply {needs}.")

        if context:
            context.observation(
                f"Derived {len(self.SECTIONS) - len(unresolved)} of "
                f"{len(self.SECTIONS)} sections from stated requirements"
            )
            context.completed()
        return EngineeringReport(
            run_id=run_id,
            task=f"design {question[:80]}",
            status="completed" if capacity else "partial",
            executive_summary=(
                f"Design worksheet for {question[:120]}. "
                f"{len(self.SECTIONS) - len(unresolved)} of {len(self.SECTIONS)} "
                "sections are derived from what you stated; the rest name the "
                "input they need rather than guessing it."
            ),
            investigated=["stated requirements", "capacity arithmetic"]
            + (["referenced workspace"] if detected else []),
            facts=facts,
            evidence_items=items,
            unknowns=unresolved,
            timeline=context.events if context else [],
            tool_calls=context.calls if context else [],
            intent="SYSTEM_DESIGN",
            stage="CONCLUDE",
            limitations=[*self.guard.limitations(), "A design worksheet is a checklist, not a decision. Capacity numbers " "are arithmetic on the values you supplied.", "Design mode does not generate implementation code."],
        )

    def _capacity(self, stated: dict) -> dict[str, float]:
        per_day = stated.get("volume_per_day")
        per_second = stated.get("volume_per_second")
        if per_second and not per_day:
            per_day = float(per_second) * 86_400
        if not per_day:
            return {}
        payload_kb = float(stated.get("payload_kb") or 0)
        average = float(per_day) / 86_400
        return {
            "events_per_day": float(per_day),
            "average_per_sec": round(average, 3),
            "peak_per_sec_at_10x": round(average * 10, 3),
            "daily_ingress_gb": round(float(per_day) * payload_kb / 1024 / 1024, 3),
            "payload_kb": payload_kb,
        }

    def _section(self, name: str, stated: dict, capacity: dict, detected: dict) -> str | None:
        if name == "Capacity estimation" and capacity:
            return (
                f"{capacity['average_per_sec']}/s average, "
                f"{capacity['peak_per_sec_at_10x']}/s at a 10x peak, "
                f"{capacity['daily_ingress_gb']} GB/day ingress."
            )
        if name == "Requirements" and stated:
            return ", ".join(f"{key}={value}" for key, value in sorted(stated.items()))
        if name == "Architecture" and detected:
            return (
                "Reuse the existing "
                + ", ".join(sorted(detected))
                + " boundaries already present in the codebase."
            )
        if name == "Observability" and detected:
            return (
                "Instrument each detected boundary: "
                + ", ".join(sorted(detected))
                + "."
            )
        if name == "Scaling" and capacity:
            driver = "payload volume" if capacity.get("daily_ingress_gb", 0) > 100 else "request rate"
            return f"{driver} scales first at the stated numbers."
        return None

    async def _scan(self, path: str) -> dict[str, list[str]]:
        try:
            workspace = open_local(path)
        except ProviderError:
            return {}
        return await _detect(RepoWorkspace(workspace), BOUNDARY_PATTERNS)


# --------------------------------------------------------------------------- #
# Observe
# --------------------------------------------------------------------------- #


async def _detect(repo: RepoWorkspace, patterns: dict[str, str]) -> dict[str, list[str]]:
    detected: dict[str, list[str]] = {}
    tree = await repo.tree()
    for path in tree[:200]:
        if not path.endswith((".py", ".rb", ".ts", ".js", ".go", ".java", ".rs")):
            continue
        try:
            text = await repo.read(path)
        except (ProviderError, AccessDenied):
            continue
        for name, pattern in patterns.items():
            if re.search(pattern, text, re.I):
                detected.setdefault(name, []).append(path)
    return detected


class ObserveWorkflow:
    """Observability gap analysis, with a finding per missing signal."""

    def __init__(self, guard: CapabilityGuard | None = None, tools=None):
        self.guard = guard or CapabilityGuard()
        self.tools = tools

    async def run(
        self, request: PlatformRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        run_id = context.run_id if context else None
        items, results = await collect_runtime(
            self.guard, request.question or "up", run_id
        )
        facts: list[str] = []
        findings: list[Finding] = []
        boundaries: dict[str, list[str]] = {}
        telemetry: dict[str, list[str]] = {}

        if request.path:
            workspace = open_local(request.path)
            repo = RepoWorkspace(workspace)
            boundaries = await _detect(repo, BOUNDARY_PATTERNS)
            telemetry = await _detect(repo, TELEMETRY_PATTERNS)
            for name, files in boundaries.items():
                facts.append(f"{name} boundary in {', '.join(files[:5])}.")
                items.append(
                    retrieved(
                        "code",
                        files[0],
                        f"{name} boundary detected in {len(files)} files.",
                        run_id=run_id,
                        path=files[0],
                    )
                )
            for signal, pattern_files in telemetry.items():
                facts.append(f"{signal} present in {len(pattern_files)} files.")
            for signal in TELEMETRY_PATTERNS:
                if signal in telemetry:
                    continue
                findings.append(
                    Finding(
                        id=f"OBS-{signal.replace(' ', '-')}",
                        severity="P2" if signal != "correlation ids" else "P3",
                        status="SUPPORTED",
                        confidence=finding_band("SUPPORTED", len(boundaries)),
                        category="observability",
                        title=f"No {signal} detected in the scanned source.",
                        production_scenario=(
                            f"When one of the {len(boundaries) or 'detected'} boundaries "
                            "fails, there is no signal to diagnose it with."
                        ),
                        evidence=[item.id for item in items if item.type == "code"][:3],
                        impact="Incidents are diagnosed by guesswork rather than by data.",
                        recommended_fix=f"Add {signal} at each detected boundary.",
                        required_test=f"Assert {signal} is emitted on the request path.",
                        verification=f"Assert {signal} is emitted on the request path.",
                    )
                )

        if not boundaries and request.path:
            facts.append("No HTTP, database, cache or queue boundaries were detected.")
        if not request.path:
            facts.append("No workspace path was supplied, so no source was scanned.")

        reached = [result for result in results if result.reached]
        if context:
            context.observation(
                f"Detected {len(boundaries)} boundaries and {len(findings)} gaps"
            )
            context.completed()
        return EngineeringReport(
            run_id=run_id,
            task=f"observe {request.path or 'workspace'}",
            status="completed" if (boundaries or reached) else "partial",
            executive_summary=(
                f"Found {len(boundaries)} service boundaries and "
                f"{len(findings)} observability gaps. "
                + (
                    f"{len(reached)} live provider(s) answered."
                    if reached
                    else "No live provider answered, so nothing was verified at runtime."
                )
            ),
            investigated=["repository markers", "runtime providers"],
            facts=facts,
            findings=findings,
            services_affected=sorted(boundaries),
            evidence_items=items,
            provider_results=results,
            timeline=context.events if context else [],
            tool_calls=context.calls if context else [],
            intent="OBSERVABILITY",
            stage="RECOMMEND",
            unknowns=[
                f"{result.provider} is unavailable: {result.error or result.status}"
                for result in results
                if not result.reached
            ],
            limitations=self.guard.limitations(),
        )
