"""Lexical change-surface analysis.

Three things the naive version got wrong, fixed here.

**Tokenisation.** ``AccountResolver`` in a ticket has to match
``account_resolver.py`` on disk, and ``p95`` has to survive as a term.
Identifiers are split on case boundaries and on separators, and alphanumeric
tokens are kept.

**Ranking.** Path-name overlap alone leaves large ties broken alphabetically,
so "the top 20 of 200 files" was effectively arbitrary. Candidates are now
shortlisted by path, scored by what their *contents* actually match, and ties
break on path depth rather than on the alphabet.

**Disclosure.** What was not inspected is recorded as a :class:`Truncation` on
the result, not merely mentioned in prose.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath

from devlens.domain import (
    AnalysisRequest,
    EvidenceItem,
    ProviderError,
    Ticket,
    Truncation,
)
from devlens.evidence import retrieved
from devlens.runtime import RunContext
from devlens.tools.context import ContextTools

EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".cs",
    ".rb", ".php", ".scala", ".swift", ".sql", ".md", ".yaml", ".yml", ".tf",
}
EXCLUDED_PARTS = {
    "node_modules", "vendor", "dist", "build", "target", "coverage", "secrets",
    ".git", ".venv", "venv", "__pycache__", "migrations", "generated", "fixtures",
}
GENERATED = re.compile(r"(\.min\.|\.lock$|_pb2\.py$|\.generated\.|\.snap$)")

STOP_WORDS = frozenset(
    {
        "this", "that", "with", "from", "when", "then", "have", "should", "does",
        "issue", "ticket", "please", "error", "user", "users", "will", "would",
        "there", "their", "which", "about", "after", "before", "also", "been",
        "need", "needs", "into", "over", "make", "made", "some", "only", "same",
    }
)

#: Shortlist size before content scoring, and evidence cap after it.
SHORTLIST = 30
MAX_FILES = 20
MAX_EVIDENCE = 20
MAX_FILE_BYTES = 100_000

_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize(text: str) -> set[str]:
    """Split text into comparable lexical terms.

    ``AccountResolver`` yields ``{account, resolver, accountresolver}`` so it
    matches both ``account_resolver.py`` and ``accountResolver.ts``; ``p95``
    survives because alphanumeric tokens are kept.
    """
    terms: set[str] = set()
    for chunk in _SPLIT.split(text or ""):
        if not chunk:
            continue
        lowered = chunk.lower()
        if len(lowered) >= 3 and lowered not in STOP_WORDS:
            terms.add(lowered)
        for part in _CAMEL.split(chunk):
            piece = part.lower()
            if len(piece) >= 3 and piece not in STOP_WORDS:
                terms.add(piece)
    return terms


# Backwards-compatible alias used across the codebase.
terms = tokenize


def eligible(path: str, size: int) -> bool:
    posix = PurePosixPath(path)
    if posix.suffix not in EXTENSIONS:
        return False
    if GENERATED.search(path):
        return False
    if any(part in EXCLUDED_PARTS or part.startswith(".") for part in posix.parts):
        return False
    return 0 < size <= MAX_FILE_BYTES


def shortlist(files, keywords: set[str]) -> list:
    """Rank candidates by path overlap, then by shallowness, then by name."""
    scored = [
        (
            -len(tokenize(item.path) & keywords),
            len(PurePosixPath(item.path).parts),
            item.path,
            item,
        )
        for item in files
        if eligible(item.path, item.size)
    ]
    scored.sort(key=lambda row: row[:3])
    return [row[3] for row in scored]


async def build_context(
    tools: ContextTools,
    request: AnalysisRequest,
    ticket: Ticket,
    context: RunContext | None = None,
) -> tuple[str, list[EvidenceItem], list[str], list[Truncation]]:
    """Resolve a ref, rank the change surface and read the strongest matches."""
    commit, files = await tools.repository(request, context=context)
    keywords = tokenize(f"{ticket.summary} {ticket.description}")
    candidates = shortlist(files, keywords)
    truncations: list[Truncation] = []
    limitations = [
        "Lexical relevance only; a term match does not establish a root cause.",
        "Only eligible text files are inspected; hidden, generated, vendored and "
        f"oversized (>{MAX_FILE_BYTES // 1000} kB) files are excluded.",
    ]
    if not candidates:
        return commit, [], limitations, truncations

    inspected = candidates[:SHORTLIST]
    if len(candidates) > SHORTLIST:
        truncations.append(
            Truncation(
                source=f"{request.repository} change surface",
                fetched=SHORTLIST,
                total=len(candidates),
                reason=f"highest path relevance of {len(files)} tracked files",
            )
        )

    async def read(item):
        try:
            return item, await tools.read_file(request, commit, item.path, context=context)
        except ProviderError:
            return item, None

    results = await asyncio.gather(*(read(item) for item in inspected))
    unreadable = [item.path for item, content in results if content is None]

    scored: list[tuple[int, int, str, object, list[tuple[int, str, list[str]]]]] = []
    for item, content in results:
        if content is None:
            continue
        hits: list[tuple[int, str, list[str]]] = []
        for number, line in enumerate(content.splitlines(), 1):
            matched = sorted(tokenize(line) & keywords)
            if matched:
                hits.append((number, line, matched))
        if hits:
            distinct = len({term for _, _, matched in hits for term in matched})
            scored.append(
                (-distinct, len(PurePosixPath(item.path).parts), item.path, item, hits)
            )
    scored.sort(key=lambda row: row[:3])

    evidence: list[EvidenceItem] = []
    for _, _, path, _item, hits in scored[:MAX_FILES]:
        for number, line, matched in sorted(
            hits, key=lambda hit: (-len(hit[2]), hit[0])
        ):
            if len(evidence) >= MAX_EVIDENCE:
                break
            evidence.append(
                retrieved(
                    "code",
                    f"{path}:{number}",
                    f"Line matches {', '.join(matched)}.",
                    status="SUPPORTED",
                    excerpt=line.strip()[:400],
                    selectors=matched,
                    run_id=context.run_id if context else None,
                    repository=request.repository,
                    commit=commit,
                    ref=request.ref,
                    path=path,
                    line=number,
                    url=tools.git.evidence_url(request.repository, commit, path, number),
                )
            )
        if len(evidence) >= MAX_EVIDENCE:
            break

    if unreadable:
        truncations.append(
            Truncation(
                source="unreadable files",
                fetched=len(inspected) - len(unreadable),
                total=len(inspected),
                reason=f"{len(unreadable)} could not be read: {', '.join(unreadable[:5])}",
            )
        )
    if len(scored) > MAX_FILES:
        truncations.append(
            Truncation(
                source="matching files",
                fetched=MAX_FILES,
                total=len(scored),
                reason="ranked by distinct matching terms",
            )
        )
    return commit, evidence, limitations, truncations
