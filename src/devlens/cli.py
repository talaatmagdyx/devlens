import argparse
import asyncio
import sys

from devlens.app.config import Settings
from devlens.app.dependencies import agent_context
from devlens.domain import AccessDenied, AnalysisRequest, ProviderError


async def run(request: AnalysisRequest) -> None:
    async with agent_context(Settings.from_env()) as agent:
        print((await agent.analyze_ticket(request)).model_dump_json(indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(prog="devlens")
    commands = parser.add_subparsers(dest="command", required=True)
    ticket = commands.add_parser(
        "ticket", help="Analyze a Jira ticket against a configured repository"
    )
    ticket.add_argument("ticket_key")
    ticket.add_argument("--project", default="default")
    ticket.add_argument(
        "--repository", required=True, help="owner/repository or workspace/repository"
    )
    ticket.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    try:
        asyncio.run(
            run(
                AnalysisRequest(
                    project=args.project,
                    ticket_key=args.ticket_key,
                    repository=args.repository,
                    ref=args.ref,
                )
            )
        )
        return 0
    except (ValueError, OSError, AccessDenied, ProviderError, TimeoutError):
        print(
            "DevLens failed: check request, configuration, access, and provider availability.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
