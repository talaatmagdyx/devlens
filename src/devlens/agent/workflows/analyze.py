"""Local checkout analysis: git only, no Jira and no remote credentials."""

from __future__ import annotations

from devlens.domain import AnalyzeRequest, EngineeringReport, EvidenceItem, ProviderError
from devlens.evidence import retrieved
from devlens.providers.git.local import RepoWorkspace
from devlens.runtime import RunContext
from devlens.sandbox.workspace import open_local

ARCHITECTURE_FILES = (
    "README.md",
    "README.rst",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "docs/README.md",
    "docs/architecture.md",
)


class AnalyzeWorkflow:
    async def run(
        self, request: AnalyzeRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        workspace = open_local(request.path)
        repo = RepoWorkspace(workspace)
        evidence: list[EvidenceItem] = []

        async def step(name: str, coro):
            if context is None:
                return await coro
            async with context.tool(name, path=str(workspace.root)) as box:
                box["result"] = result = await coro
                return result

        tree = await step("local.tree", repo.tree())
        status = (await step("local.status", repo.status())).strip()
        log = (await step("local.log", repo.log())).strip()
        show = (await step("local.show", repo.show())).strip()
        diff = (await step("local.diff", repo.diff())).strip()

        facts = [f"Workspace {workspace.root} lists {len(tree)} files."]
        if log:
            facts.append("Recent commits:\n" + log)
            evidence.append(
                retrieved(
                    "git",
                    str(workspace.root),
                    f"HEAD history: {len(log.splitlines())} recent commits.",
                    excerpt=log[:1500],
                    run_id=context.run_id if context else None,
                    path=".git",
                )
            )
        if show:
            facts.append("HEAD commit:\n" + show[:2000])
        facts.append(
            "Working tree status:\n" + status if status else "Working tree is clean."
        )
        if diff:
            facts.append("Working tree diff:\n" + diff[:4000])

        observations: list[str] = []
        if request.query:
            hits = await step("local.search", repo.search(request.query))
            observations.append(
                f"Search for {request.query!r}:\n{hits[:4000] or 'No matches.'}"
            )
            if hits.strip():
                first = hits.splitlines()[0]
                path = first.split(":", 1)[0] if ":" in first else ""
                evidence.append(
                    retrieved(
                        "code",
                        path or str(workspace.root),
                        f"{len(hits.splitlines())} lines match {request.query!r}.",
                        excerpt=hits[:1500],
                        selectors=[request.query],
                        run_id=context.run_id if context else None,
                        path=path or None,
                    )
                )
                if path:
                    try:
                        blame = (await repo.blame(path)).strip()
                    except ProviderError:
                        blame = ""
                    if blame:
                        observations.append(f"Blame for {path}:\n{blame[:1500]}")

        for name in ARCHITECTURE_FILES:
            try:
                text = (await repo.read(name)).strip()
            except (ProviderError, OSError):
                continue
            if text:
                observations.append(f"Notes from {name}:\n{text[:2000]}")
                evidence.append(
                    retrieved(
                        "document",
                        name,
                        f"{name} documents the project's intent.",
                        excerpt=text[:1000],
                        run_id=context.run_id if context else None,
                        path=name,
                    )
                )

        markers = await repo.markers()
        detected = ", ".join(name for name, present in markers.items() if present)
        observations.append(f"Detected test runners: {detected or 'none'}.")
        if context:
            context.observation(f"Inspected {len(tree)} files in {workspace.root}")

        return EngineeringReport(
            run_id=context.run_id if context else None,
            task=f"analyze {request.path}",
            status="completed" if tree else "insufficient_context",
            executive_summary=(
                f"Inspected the local repository at {workspace.root}: {len(tree)} files, "
                f"{len(log.splitlines())} recent commits, "
                f"{'a dirty' if status else 'a clean'} working tree."
            ),
            investigated=[
                "local git workspace",
                "file tree",
                "git log",
                "git show",
                "git diff",
                "git status",
                "architecture files",
            ],
            facts=facts,
            observations=observations,
            evidence_items=evidence,
            files_affected=tree[:50],
            timeline=context.events if context else [],
            tool_calls=context.calls if context else [],
            limitations=[
                "Local analysis is read-only and calls no remote API.",
                "Vendored, generated and hidden directories are excluded from the tree and search.",
                "Findings are lexical; they do not establish a root cause.",
            ],
        )
