import asyncio
from pathlib import PurePosixPath
import re

from devlens.domain import AnalysisRequest, Evidence, ProviderError, Ticket
from devlens.tools.context import ContextTools

EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".rb",
    ".sql",
    ".md",
}
EXCLUDED = {"node_modules", "vendor", "dist", "build", "secrets", ".git", ".venv"}
STOP_WORDS = {
    "this",
    "that",
    "with",
    "from",
    "when",
    "then",
    "have",
    "should",
    "does",
    "issue",
    "ticket",
    "please",
    "error",
}


def terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_]{3,}", text.lower())) - STOP_WORDS


async def build_context(tools: ContextTools, request: AnalysisRequest, ticket: Ticket):
    commit, files = await tools.repository(request)
    keywords = terms(ticket.summary + " " + ticket.description)
    candidates = [
        f
        for f in files
        if PurePosixPath(f.path).suffix in EXTENSIONS
        and not any(
            part in EXCLUDED or part.startswith(".")
            for part in PurePosixPath(f.path).parts
        )
        and 0 < f.size <= 100_000
    ]
    candidates.sort(key=lambda f: (-len(terms(f.path) & keywords), f.path))
    limitations = [
        "Lexical relevance only; findings do not establish a root cause.",
        "Only eligible text files are inspected; hidden, generated, and oversized files are excluded.",
    ]
    if len(candidates) > 20:
        limitations.append(f"Inspected the top 20 of {len(candidates)} eligible files.")
    evidence = []

    async def inspect(file):
        try:
            return await tools.read_file(request, commit, file.path)
        except ProviderError:
            return None

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(inspect(file)) for file in candidates[:20]]
    for file, task in zip(candidates[:20], tasks):
        content = task.result()
        if content is None:
            limitations.append(f"Could not inspect {file.path}.")
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            matches = sorted(terms(line) & keywords)
            if matches:
                evidence.append(
                    Evidence(
                        path=file.path,
                        line=line_number,
                        excerpt=line[:400],
                        matched_terms=matches,
                        url=tools.git.evidence_url(
                            request.repository, commit, file.path, line_number
                        ),
                    )
                )
    evidence.sort(key=lambda item: (-len(item.matched_terms), item.path, item.line))
    if len(evidence) > 20:
        limitations.append("Evidence is limited to the 20 strongest matching lines.")
    return commit, evidence[:20], limitations
