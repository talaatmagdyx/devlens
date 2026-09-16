"""Implementation planning.

The boundary is explicit and has not moved: **DevLens does not generate
application code.** It produces a change-surface plan grounded in the ticket and
the repository, optionally prepares a local sandbox branch, and optionally runs
the repository's existing tests to establish a baseline.

What it does now that it did not before is offer *proposals* — a branch and a
Jira comment carrying the plan — so that if an operator approves one, something
meaningful is written rather than a placeholder.
"""

from __future__ import annotations

import time
from uuid import uuid4

from devlens.agent.context_builder import build_context
from devlens.domain import (
    AccessDenied,
    AnalysisRequest,
    EngineeringReport,
    ImplementRequest,
    Proposal,
    ProviderError,
)
from devlens.providers.git.local import RepoWorkspace
from devlens.providers.writes import branch_for
from devlens.runtime import RunContext
from devlens.tools.context import ContextTools
from devlens.tools.review_heuristics import review_changes
from devlens.tools.shell import Limits, run_repository_code


class ImplementWorkflow:
    def __init__(self, tools: ContextTools):
        self.tools = tools

    async def run(
        self, request: ImplementRequest, context: RunContext | None = None
    ) -> EngineeringReport:
        issue = await self.tools.issue_context(request, context=context)
        analysis = AnalysisRequest(
            project=request.project,
            ticket_key=request.ticket_key,
            repository=request.repository,
            ref=request.ref,
        )
        commit, evidence, limitations, truncations = await build_context(
            self.tools, analysis, issue, context=context
        )
        # What the ticket read did not cover has to reach the report too: a plan
        # built from 30 of 900 comments must say so.
        truncations = [*issue.truncations, *truncations]
        files = sorted({item.provenance.path for item in evidence if item.provenance.path})

        plan = [f"Read {path} and locate the behaviour described by the ticket." for path in files[:10]]
        if issue.spec.expected_behavior:
            plan.append(f"Target behaviour: {issue.spec.expected_behavior}")
        if issue.spec.acceptance_criteria:
            plan.extend(
                f"Satisfy acceptance criterion: {item}"
                for item in issue.spec.acceptance_criteria[:8]
            )
        else:
            plan.append(
                "No acceptance criteria are stated. Agree them before writing code "
                "rather than inferring them."
            )
        plan.append("Add or update tests covering the changed behaviour.")
        plan.append(
            "Do not push, open a pull request or comment on Jira without an "
            "approved proposal."
        )

        facts = [
            f"Jira {issue.key}: {issue.summary}",
            f"Resolved {request.repository}@{commit}.",
            f"Lexical change surface spans {len(files)} files.",
        ]
        facts.extend(f"Missing from the ticket: {item}" for item in issue.spec.missing_information)

        observations: list[str] = []
        branch = branch_for(request.ticket_key, request.repository)
        try:
            async with await self.tools.open_workspace(
                request.repository, request.ref, context=context
            ) as workspace:
                await workspace.create_local_branch(branch)
                repo = RepoWorkspace(workspace)
                observations.append(
                    f"Created local sandbox branch {branch}. It was not pushed."
                )
                diff = await repo.diff()
                findings = review_changes(diff, []) if diff.strip() else []
                observations.append(
                    "Reviewed the sandbox working tree."
                    if diff.strip()
                    else "Sandbox working tree is clean; there is nothing to review yet."
                )
                if request.run_tests:
                    observations.extend(
                        await self._baseline(workspace, repo, context)
                    )
        except AccessDenied:
            raise
        except (ProviderError, OSError, RuntimeError) as exc:
            findings = []
            limitations = [
                *limitations,
                f"A sandbox clone was not available ({type(exc).__name__}: {exc}).",
            ]

        proposals = self._proposals(request, issue, plan, files, branch, commit, context)
        if context:
            context.observation(f"Planned {len(plan)} steps over {len(files)} files")
            context.completed()

        return EngineeringReport(
            run_id=context.run_id if context else None,
            task=f"implement {request.ticket_key}",
            status="completed",
            executive_summary=(
                f"Prepared a change-surface plan for {request.ticket_key} covering "
                f"{len(files)} files at {commit[:12]}. No application code was "
                "generated and no remote was modified."
            ),
            investigated=["jira issue", "lexical change surface", "local sandbox"],
            facts=facts,
            observations=observations,
            findings=findings,
            evidence_items=evidence,
            proposals=proposals,
            timeline=context.events if context else [],
            tool_calls=context.calls if context else [],
            unknowns=issue.spec.missing_information,
            implementation_plan=plan,
            verification_plan=[
                "Run the existing test suite after any human-authored change.",
                "Re-run review heuristics on the resulting diff.",
            ],
            risks=(
                ["Implementing without stated acceptance criteria invents requirements."]
                if not issue.spec.acceptance_criteria
                else []
            ),
            files_affected=files,
            truncations=truncations,
            limitations=[*limitations, "This workflow does not generate application code.", "Remote writes require an approved proposal; merge and deploy stay human-only."],
            ticket=issue,
        )

    async def _baseline(self, workspace, repo, context) -> list[str]:
        guard = self.tools.guard
        if guard is None or not guard.has("repository_code_execution"):
            return [
                "Baseline tests were not run: executing repository code is disabled "
                "(set DEVLENS_ALLOW_REPO_TESTS=1 for repositories you trust)."
            ]
        markers = await repo.markers()
        if markers["pytest"]:
            argv = ["pytest", "-q", "-p", "no:cacheprovider"]
        elif markers["rspec"]:
            argv = ["bundle", "exec", "rspec", "--no-color"]
        else:
            return ["No recognised test runner in the sandbox."]
        try:
            result = await run_repository_code(
                argv, cwd=workspace.root, guard=guard, limits=Limits.for_tests()
            )
        except (AccessDenied, ProviderError) as exc:
            return [f"Baseline tests were not run: {exc}"]
        return [
            f"Baseline test run exited {result.returncode} in {result.duration_ms} ms."
        ]

    def _proposals(
        self, request, issue, plan, files, branch, commit, context
    ) -> list[Proposal]:
        run_id = context.run_id if context else "cli"
        body = "\n\n".join(
            [
                f"**DevLens implementation plan for {issue.key}**",
                f"Change surface: {len(files)} files at `{commit[:12]}`.",
                "\n".join(f"{index}. {step}" for index, step in enumerate(plan, 1)),
                "DevLens did not generate application code. This plan is a starting "
                "point for a human change.",
            ]
        )
        return [
            Proposal(
                id=str(uuid4()),
                run_id=run_id,
                action="create_remote_branch",
                repository=request.repository,
                ticket_key=request.ticket_key,
                target=f"{request.repository}@{branch}",
                payload={"branch": branch, "sha": commit},
                rationale=f"Create a working branch for {issue.key} at the analysed commit.",
                created_at=time.time(),
            ),
            Proposal(
                id=str(uuid4()),
                run_id=run_id,
                action="post_jira_comment",
                ticket_key=request.ticket_key,
                target=request.ticket_key,
                payload={"key": request.ticket_key, "body": body},
                rationale="Publish the change-surface plan to the ticket.",
                created_at=time.time(),
            ),
        ]
