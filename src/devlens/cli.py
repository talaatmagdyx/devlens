"""Command-line interface.

The CLI runs the same workflows as the API with the same capability guard, so
``devlens ask`` behaves identically to the Ask surface. Errors report what
actually failed and which category it falls into, rather than a single generic
line that gives an operator nothing to act on.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from devlens.agent.workflows.analyze import AnalyzeWorkflow
from devlens.agent.workflows.platform import (
    AskWorkflow,
    DesignWorkflow,
    InvestigateWorkflow,
    ObserveWorkflow,
)
from devlens.app.config import Settings
from devlens.app.dependencies import agent_context
from devlens.app.jobs import classify
from devlens.domain import (
    DEVLENS_VERSION,
    AccessDenied,
    AnalysisRequest,
    AnalyzeRequest,
    ImplementRequest,
    PlatformRequest,
    ProviderError,
    ReviewRequest,
)
from devlens.guardrails import CapabilityGuard
from devlens.runtime import RunContext

PLATFORM: dict[
    str, type[AskWorkflow | DesignWorkflow | InvestigateWorkflow | ObserveWorkflow]
] = {
    "ask": AskWorkflow,
    "investigate": InvestigateWorkflow,
    "design": DesignWorkflow,
    "observe": ObserveWorkflow,
}


def _print(result) -> None:
    print(result.model_dump_json(indent=2))


async def run_configured(command: str, request) -> None:
    async with agent_context(Settings.from_env()) as runners:
        context = RunContext(capabilities=sorted(runners.guard.enabled) if runners.guard else [])
        method = {"ticket": "analyze_ticket"}.get(command, command)
        _print(await getattr(runners, method)(request, context=context))


async def run_local(request: AnalyzeRequest) -> None:
    _print(await AnalyzeWorkflow().run(request, context=RunContext()))


async def run_platform(command: str, request: PlatformRequest) -> None:
    """Platform commands use whatever capabilities the environment enables.

    Previously these always ran with an empty guard, so a configured model or
    telemetry backend was silently ignored on the CLI.
    """
    guard = CapabilityGuard.from_env()
    context = RunContext(capabilities=sorted(guard.enabled))
    try:
        async with agent_context(Settings.from_env(), guard) as runners:
            _print(await getattr(runners, command)(request, context=context))
            return
    except (ValueError, OSError):
        pass
    _print(await PLATFORM[command](guard).run(request, context=context))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devlens", description="Evidence-first engineering analysis."
    )
    parser.add_argument("--version", action="version", version=DEVLENS_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    ticket = commands.add_parser("ticket", help="Analyse a Jira ticket against a repository")
    ticket.add_argument("ticket_key")
    ticket.add_argument("--project", default="default")
    ticket.add_argument("--repository", required=True, help="owner/repo or workspace/repo")
    ticket.add_argument("--ref", default="HEAD")

    analyze = commands.add_parser("analyze", help="Inspect a local git checkout")
    analyze.add_argument("path")
    analyze.add_argument("--query")

    review = commands.add_parser("review", help="Heuristically review a pull request")
    review.add_argument("ticket_key", nargs="?")
    review.add_argument("--project", default="default")
    review.add_argument("--repository", "--repo", dest="repository", required=True)
    review.add_argument("--pr", type=int, required=True)
    review.add_argument(
        "--run-tests",
        action="store_true",
        help="run the repository's own tests (requires DEVLENS_ALLOW_REPO_TESTS=1)",
    )

    implement = commands.add_parser("implement", help="Plan an implementation")
    implement.add_argument("ticket_key")
    implement.add_argument("--project", default="default")
    implement.add_argument("--repository", required=True)
    implement.add_argument("--ref", default="HEAD")
    implement.add_argument("--run-tests", action="store_true")

    for name, help_text in (
        ("ask", "Ask an evidence-backed engineering question"),
        ("investigate", "Investigate a ticket or symptom"),
        ("design", "Produce a structured design worksheet"),
        ("observe", "Observability gap report"),
    ):
        item = commands.add_parser(name, help=help_text)
        item.add_argument("question", nargs="?")
        item.add_argument("--path")
        item.add_argument("--ticket")
        item.add_argument("--repository")
        item.add_argument("--project", default="default")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` (defaulting to ``sys.argv``) and run one command.

    Taking ``argv`` as an argument is what makes the CLI testable without
    monkeypatching ``sys.argv``; ``__main__`` still passes nothing.
    """
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ticket":
            asyncio.run(
                run_configured(
                    "ticket",
                    AnalysisRequest(
                        project=args.project,
                        ticket_key=args.ticket_key,
                        repository=args.repository,
                        ref=args.ref,
                    ),
                )
            )
        elif args.command == "analyze":
            asyncio.run(run_local(AnalyzeRequest(path=args.path, query=args.query)))
        elif args.command == "review":
            asyncio.run(
                run_configured(
                    "review",
                    ReviewRequest(
                        project=args.project,
                        ticket_key=args.ticket_key,
                        repository=args.repository,
                        pr=args.pr,
                        run_tests=args.run_tests,
                    ),
                )
            )
        elif args.command == "implement":
            asyncio.run(
                run_configured(
                    "implement",
                    ImplementRequest(
                        project=args.project,
                        ticket_key=args.ticket_key,
                        repository=args.repository,
                        ref=args.ref,
                        run_tests=args.run_tests,
                    ),
                )
            )
        else:
            asyncio.run(
                run_platform(
                    args.command,
                    PlatformRequest(
                        project=args.project,
                        question=args.question,
                        path=args.path,
                        ticket_key=args.ticket,
                        repository=args.repository,
                    ),
                )
            )
        return 0
    except (AccessDenied, ProviderError, TimeoutError, ValueError, OSError) as exc:
        kind, message = classify(exc)
        print(f"devlens: {kind}: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
