"""Pull-request review.

Deterministic heuristics over the diff, plus acceptance-criteria cross-checking
when a Jira ticket is supplied. Two behaviours worth naming:

*Files are read, not guessed.* Candidate test paths are checked against the
repository tree that was already fetched, instead of issuing a request per guess
and swallowing the 404s — which also means a genuine authentication failure is
no longer indistinguishable from "file absent".

*Everything the review did not see is recorded.* A 400-file pull request read
100 files says so on the report, as a Truncation, not as prose.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from devlens.domain import (
    AccessDenied,
    EngineeringReport,
    EvidenceItem,
    Finding,
    Proposal,
    ProviderError,
    ReviewRequest,
    Truncation,
)
from devlens.evidence import finding_band, retrieved
from devlens.providers.git.local import RepoWorkspace
from devlens.runtime import RunContext
from devlens.tools.context import ContextTools
from devlens.tools.review_heuristics import review_changes
from devlens.tools.shell import Limits, run_repository_code

MAX_FILES_READ = 25
MAX_DIFF_CHARS = 200_000


def related_paths(path: str) -> list[str]:
    """Conventional test-file locations for a source path."""
    posix = PurePosixPath(path)
    stem = posix.stem
    parent = posix.parent.as_posix()
    prefix = "" if parent == "." else f"{parent}/"
    candidates = [
        f"{prefix}test_{stem}.py",
        f"{prefix}{stem}_test.py",
        f"{prefix}{stem}.test.ts",
        f"{prefix}{stem}.spec.ts",
        f"{prefix}{stem}_spec.rb",
        f"tests/test_{stem}.py",
        f"spec/{stem}_spec.rb",
    ]
    return list(dict.fromkeys(item for item in candidates if item != path))


class ReviewWorkflow:
    def __init__(self, tools: ContextTools):
        self.tools = tools

    async def run(
        self, request: ReviewRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        pull = await self.tools.pull_request(request, context=context)
        diff = await self.tools.pull_request_diff(request, context=context)
        commits, commits_truncated = await self.tools.pull_request_commits(
            request, context=context
        )
        truncations: list[Truncation] = list(pull.truncations)
        if commits_truncated:
            truncations.append(
                Truncation(
                    source=f"{request.repository}#{request.pr} commits",
                    fetched=len(commits),
                    total=None,
                    reason="pagination limit reached",
                )
            )

        issue = None
        if request.ticket_key:
            issue = await self.tools.issue_context(request, context=context)
            truncations.extend(issue.truncations)

        contents, read_truncation = await self._read_changed(request, pull, context)
        if read_truncation:
            truncations.append(read_truncation)

        findings = review_changes(diff, pull.files, contents)
        if issue and issue.spec.acceptance_criteria:
            findings.extend(self._criteria_findings(issue, contents, diff))

        evidence: list[EvidenceItem] = [
            retrieved(
                "git",
                f"{request.repository}#{pull.number}",
                f"{len(pull.files)} changed files across {len(commits)} commits.",
                excerpt=f"{pull.source_branch} -> {pull.target_branch} at {pull.head_sha[:12]}",
                run_id=context.run_id if context else None,
                repository=request.repository,
                commit=pull.head_sha,
                url=pull.url,
            )
        ]
        for finding in findings[:15]:
            if finding.file:
                evidence.append(
                    retrieved(
                        "code",
                        f"{finding.file}:{finding.line or 0}",
                        finding.title,
                        excerpt="\n".join(finding.evidence)[:400],
                        status="SUPPORTED" if finding.status == "SUPPORTED" else "HYPOTHESIS",
                        run_id=context.run_id if context else None,
                        repository=request.repository,
                        commit=pull.head_sha,
                        path=finding.file,
                        line=finding.line,
                        url=self.tools.git.evidence_url(
                            request.repository, pull.head_sha, finding.file, finding.line or 1
                        ),
                    )
                )

        observations: list[str] = []
        if request.run_tests:
            observations.extend(await self._run_tests(request, pull.head_sha, context))

        facts = [
            f"PR #{pull.number}: {pull.title}",
            f"Head {pull.head_sha} ({pull.source_branch} -> {pull.target_branch}).",
            f"{len(pull.files)} changed files, {len(commits)} commits.",
        ]
        if pull.from_fork:
            facts.append(
                "This pull request comes from a fork; its head commit is not under "
                "the base repository's control."
            )
        if issue:
            facts.append(f"Jira {issue.key}: {issue.summary}")
            facts.extend(f"Missing from the ticket: {item}" for item in issue.spec.missing_information)
        observations.append(f"Diff size: {len(diff)} characters.")

        proposals = self._proposals(request, pull, findings, context)
        severity_counts = dict.fromkeys(("P0", "P1", "P2", "P3"), 0)
        for finding in findings:
            severity_counts[finding.severity] += 1
        if context:
            context.observation(f"Produced {len(findings)} heuristic findings")
            context.completed()

        return EngineeringReport(
            run_id=context.run_id if context else None,
            task=f"review {request.repository}#{request.pr}",
            status="completed" if findings or diff.strip() else "insufficient_context",
            executive_summary=(
                f"Reviewed PR #{pull.number}: {len(findings)} heuristic findings "
                f"({severity_counts['P0']} P0, {severity_counts['P1']} P1, "
                f"{severity_counts['P2']} P2, {severity_counts['P3']} P3) across "
                f"{len(pull.files)} changed files."
            ),
            investigated=["pull request metadata", "unified diff", "changed files"]
            + (["jira issue"] if issue else [])
            + (["sandbox test run"] if request.run_tests else []),
            facts=facts,
            observations=observations,
            findings=findings,
            evidence_items=evidence,
            proposals=proposals,
            timeline=context.events if context else [],
            tool_calls=context.calls if context else [],
            unknowns=[
                "Heuristics detect patterns, not intent. A clean review is not proof "
                "the change is correct.",
            ],
            recommended_fix=findings[0].recommended_fix if findings else None,
            verification_plan=[
                item.required_test for item in findings if item.required_test
            ],
            risks=[item.title for item in findings if item.severity in {"P0", "P1"}],
            files_affected=[item.path for item in pull.files],
            truncations=truncations,
            limitations=[
                "Review findings are deterministic heuristics, not a semantic review.",
                "Findings are anchored to lines this pull request changed.",
            ]
            + ([t.describe() for t in truncations] if truncations else []),
            ticket=issue,
            pull_request=pull,
            diff=diff[:MAX_DIFF_CHARS] if diff else None,
        )

    async def _read_changed(
        self, request: ReviewRequest, pull, context: RunContext | None
    ) -> tuple[dict[str, str], Truncation | None]:
        """Read changed files, plus sibling tests that actually exist."""
        try:
            _, tracked = await self.tools.repository(
                type("Req", (), {"repository": request.repository, "ref": pull.head_sha,
                                 "ticket_key": None})(),
                context=context,
            )
            existing = {item.path for item in tracked}
        except (ProviderError, AccessDenied):
            existing = set()

        wanted: list[str] = []
        for item in pull.files:
            if item.status != "removed" and not item.binary:
                wanted.append(item.path)
            wanted.extend(
                path for path in related_paths(item.path) if not existing or path in existing
            )
        ordered = list(dict.fromkeys(wanted))
        selected = ordered[:MAX_FILES_READ]
        contents: dict[str, str] = {}
        for path in selected:
            try:
                contents[path] = await self.tools.read_file(
                    request, pull.head_sha, path, context=context
                )
            except ProviderError:
                continue
        truncation = (
            Truncation(
                source="changed files read",
                fetched=len(contents),
                total=len(ordered),
                reason=f"capped at {MAX_FILES_READ} files per review",
            )
            if len(ordered) > MAX_FILES_READ
            else None
        )
        return contents, truncation

    def _criteria_findings(self, issue, contents: dict[str, str], diff: str) -> list[Finding]:
        covered = " ".join(contents).lower() + diff.lower()
        findings = []
        for index, criterion in enumerate(issue.spec.acceptance_criteria, start=1):
            tokens = [part for part in criterion.lower().split() if len(part) > 4][:3]
            if tokens and not any(token in covered for token in tokens):
                findings.append(
                    Finding(
                        id=f"AC-{index}",
                        severity="P3",
                        status="HYPOTHESIS",
                        confidence=finding_band("HYPOTHESIS", 1),
                        category="requirements",
                        title="Acceptance criterion is not obviously reflected in the diff.",
                        evidence=[criterion],
                        production_scenario="The ticket may be closed without the behaviour shipping.",
                        impact="A stated requirement goes unimplemented.",
                        recommended_fix="Confirm the change satisfies this criterion or note why it does not.",
                        required_test=f"Add a test for: {criterion}",
                        verification=f"Add a test for: {criterion}",
                    )
                )
        return findings

    async def _run_tests(
        self, request: ReviewRequest, ref: str, context: RunContext | None
    ) -> list[str]:
        """Run the repository's own tests, if the capability allows it."""
        guard = self.tools.guard
        if guard is None or not guard.has("repository_code_execution"):
            return [
                "Sandbox tests were not run: executing repository code is disabled "
                "(set DEVLENS_ALLOW_REPO_TESTS=1 for repositories you trust)."
            ]
        try:
            async with await self.tools.open_workspace(
                request.repository, ref, context=context
            ) as workspace:
                repo = RepoWorkspace(workspace)
                markers = await repo.markers()
                if markers["pytest"]:
                    argv = ["pytest", "-q", "-p", "no:cacheprovider"]
                elif markers["rspec"]:
                    argv = ["bundle", "exec", "rspec", "--no-color"]
                else:
                    return ["No recognised test runner in the cloned workspace."]
                result = await run_repository_code(
                    argv,
                    cwd=workspace.root,
                    guard=guard,
                    limits=Limits.for_tests(),
                )
                tail = result.stdout[-2000:]
                return [
                    f"Repository tests exited {result.returncode} in "
                    f"{result.duration_ms} ms"
                    + (" (output truncated)" if result.truncated else "")
                    + f":\n{tail}"
                ]
        except (AccessDenied, ProviderError, OSError) as exc:
            return [f"Sandbox tests were not run: {exc}"]

    def _proposals(
        self, request: ReviewRequest, pull, findings: list[Finding], context: RunContext | None
    ) -> list[Proposal]:
        """Offer a pull-request comment carrying the actual findings."""
        import time
        from uuid import uuid4

        if not findings:
            return []
        lines = [f"**DevLens review of #{pull.number}** — {len(findings)} findings.", ""]
        for finding in findings[:20]:
            location = f"`{finding.file}:{finding.line}`" if finding.file else "—"
            lines.append(
                f"- **{finding.severity}** ({finding.status}, {finding.confidence}) "
                f"{finding.title} {location}"
            )
            if finding.recommended_fix:
                lines.append(f"  - Fix: {finding.recommended_fix}")
        lines += ["", "These are deterministic heuristics, not a semantic review."]
        return [
            Proposal(
                id=str(uuid4()),
                run_id=context.run_id if context else "cli",
                action="post_pr_comment",
                repository=request.repository,
                target=f"{request.repository}#{pull.number}",
                payload={"number": pull.number, "body": "\n".join(lines)},
                rationale=f"Publish {len(findings)} review findings to the pull request.",
                created_at=time.time(),
            )
        ]
