# DevLens

DevLens connects a Jira Cloud ticket to relevant GitHub or Bitbucket Cloud source lines. Phase one
provides a read-only workflow, typed results, a CLI, and a FastAPI service.

## Run locally

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Set the environment variables listed in `.env.example` in your shell. DevLens
does not automatically load `.env` files. Use a Jira email/API token and a GitHub
token with read access to the configured repositories. Both repository and Jira
project allowlists are required; separate multiple entries with commas.

```sh
devlens ticket DEV-123 --repository your-org/your-repo --ref main
uvicorn devlens.app.api:app --host 127.0.0.1 --port 8000
```

API documentation is available at `http://127.0.0.1:8000/docs`.

## Multiple projects and Bitbucket Cloud

Copy `devlens.example.toml` to `devlens.toml`, configure each project's Jira site,
Git provider, repository allowlist, and Jira project keys, then set
`DEVLENS_CONFIG` to its path. Set the credential environment variables referenced
by `jira_token_env` and `git_token_env`; tokens stay out of the TOML file.
When `DEVLENS_CONFIG` is set, it replaces the legacy single-project configuration.

```sh
export DEVLENS_CONFIG="$PWD/devlens.toml"
devlens ticket PAY-123 --project payments --repository your-workspace/payments
```

The API accepts the same project selector:

```json
{"project":"payments","ticket_key":"PAY-123","repository":"your-workspace/payments","ref":"HEAD"}
```

Each named DevLens project has independent credentials and can allow multiple
repositories and Jira project keys. Repository selection is explicit per request;
one request analyzes one repository. Bitbucket Cloud identifies repositories by
`workspace/repo_slug`, not by the Bitbucket project key. The configured allowlist
can include repositories from multiple Bitbucket projects or workspaces accessible
to that credential. Automatic Bitbucket project discovery and cross-repository
analysis are not implemented. Unknown projects and disallowed repositories return 403.

Bitbucket Cloud supports an Atlassian email plus API token (`git_email` and
`git_token_env`), or a Bearer access token (omit `git_email`). API tokens require
repository read access. See [Atlassian's authentication documentation](https://developer.atlassian.com/cloud/bitbucket/rest/).
`HEAD` resolves the default branch. Directory listings follow pagination and
subdirectories, with a 500-page limit. Evidence links point to the resolved commit.
Data Center is not included.

```sh
curl -X POST http://127.0.0.1:8000/analyses/ticket \
  -H 'Content-Type: application/json' \
  -d '{"ticket_key":"DEV-123","repository":"your-org/your-repo","ref":"main"}'
python -m pytest
```

The HTTP API is a local development interface without caller authentication.
Do not expose it publicly; allowlists constrain provider access but do not
authenticate callers. For Docker, bind the published port to loopback:

```sh
docker build -t devlens .
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 devlens
```

## Architecture

`app` and `cli` wire an `EngineeringAgent` to a ticket workflow. The workflow
uses policy-checked context tools, which delegate to provider protocols and
Jira Cloud/GitHub/Bitbucket Cloud adapters. Domain types carry requests,
ticket data, evidence, and results. All provider I/O, tools, workflows, and HTTP
handlers are async. Pure validation, policies, and text ranking remain synchronous.
The CLI enters the async runtime with `asyncio.run()`.

The API creates a separate pair of HTTP connection pools for each configured
project during application startup and closes them at shutdown. Configuration
changes require restarting the API. `/health` reports process liveness;
`/ready` returns 503 when configuration is missing or invalid.

`concurrency` caps active provider operations per project across all requests
(default 5). File reads run concurrently within that cap and evidence ordering
remains deterministic. `max_analyses` caps active analyses per API worker across
projects (default 8). `analysis_timeout` limits total request processing, including
slot waiting (default 120 seconds); timeout returns 504 and cancels child work.
HTTP calls have a 15-second operation timeout and a 5-second connection timeout.
Limits are per process, so additional server workers multiply them. Provider rate
limits still apply; this release does not automatically retry 429 responses.

The workflow resolves the requested ref once, lists the tree at that commit,
ranks eligible paths using ticket terms, and reads at most 20 files of at most
100 KB each. It returns up to 20 matching source lines with commit-pinned links.
Hidden paths, common generated directories, symlinks, binary files, and unsupported
extensions are excluded. A truncated GitHub tree fails explicitly. Individual
file failures and inspection limits appear in the result's limitations.

This is deterministic lexical analysis, not an LLM diagnosis or an exhaustive
repository search. No matching evidence produces `insufficient_context`. Source
excerpts may contain sensitive information; this phase does not redact source
content. Remote content is never executed, and no write tools are registered.

Provider endpoints follow the official
[Jira issue API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/),
[GitHub trees API](https://docs.github.com/en/rest/git/trees), and
[GitHub contents API](https://docs.github.com/en/rest/repos/contents).

## Next phases

Add semantic retrieval and an evidence-constrained LLM analysis interface, then
additional workflows and providers. Durable execution state, user-triggered cancellation,
telemetry, authenticated multi-user access, sandboxed execution, catalog, and
knowledge ingestion are deferred until their first concrete use case.

Tests use async HTTP transport fixtures and cover the adapters, workflow, API,
pagination, project isolation, shared concurrency bounds, and cancellation.
Live integrations require your credentials and have not been verified by the
offline suite. No production throughput benchmark has been performed.
