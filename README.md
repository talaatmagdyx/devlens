# DevLens

DevLens reads Jira Cloud tickets, GitHub or Bitbucket Cloud repositories and
local git checkouts, and produces a report that states what it actually
retrieved, what that establishes, and what it could not check.

The design constraint is narrow and the whole tool follows from it: **DevLens
does not present something as supported unless it retrieved it.** A provider
that cannot be reached produces `UNKNOWN` evidence, not a gap quietly filled in.
A hypothesis is settled from evidence or left open. Certainty is expressed as
Low / Medium / High / Very High, never as a percentage.

Everything optional is off until it is switched on: no language model, no live
telemetry, no remote writes, no execution of a repository's own code. `GET
/capabilities` reports what is on, what is off, and the variable that changes
each one.

```sh
devlens analyze ./backend --query AccountResolver          # no credentials needed
devlens ticket DEV-123 --repository your-org/your-repo --ref main
devlens review --repository your-org/your-repo --pr 4320
devlens implement DEV-123 --repository your-org/your-repo
devlens ask "why is checkout slow?" --path ./backend
devlens investigate "p99 latency tripled after the deploy"
devlens design "10k events per second, 2kb payload"
devlens observe --path ./backend
```

---

## Contents

- [What DevLens produces](#what-devlens-produces)
- [Quick start](#quick-start)
- [Docker](#docker)
- [Configuration](#configuration)
- [Capabilities](#capabilities)
- [Language models (optional)](#language-models-optional)
- [Observability (optional)](#observability-optional)
- [Write-back and approvals (optional)](#write-back-and-approvals-optional)
- [Executing repository code (optional)](#executing-repository-code-optional)
- [Operator UI](#operator-ui)
- [CLI reference](#cli-reference)
- [HTTP API](#http-api)
- [Multiple projects and Bitbucket Cloud](#multiple-projects-and-bitbucket-cloud)
- [Hosted, multi-tenant mode](#hosted-multi-tenant-mode)
- [Architecture](#architecture)
- [Security](#security)
- [Development and tests](#development-and-tests)
- [Explicitly out of scope](#explicitly-out-of-scope)

---

## What DevLens produces

Three scales are kept separate, because collapsing them is how a tool starts
overstating itself:

| Scale | Question it answers | Values |
|---|---|---|
| **Severity** | How bad is this finding? | `P0` `P1` `P2` `P3` |
| **Evidence status** | What does the evidence establish? | `CONFIRMED` `SUPPORTED` `HYPOTHESIS` `UNKNOWN` `REJECTED` |
| **Confidence** | How sure is DevLens? | `LOW` `MEDIUM` `HIGH` `VERY_HIGH` |

Rules the code enforces, not just the prose:

- Evidence cannot claim `CONFIRMED`, `SUPPORTED` or `REJECTED` unless its
  provenance records a successful retrieval. A provider error is `UNKNOWN`.
- An image can never be `CONFIRMED` or `REJECTED`. A screenshot records a
  symptom; it cannot establish a cause.
- A hypothesis cannot be `supported` without supporting evidence, or `rejected`
  without contradicting evidence, and an open hypothesis cannot carry high
  confidence.
- Runtime evidence (metrics, logs, traces) can confirm. Code and git evidence can
  support, never confirm.
- Every report carries the limitations that applied to that run, derived from the
  capability state at run time.

| Mode | Output |
|---|---|
| `ticket` | `AnalysisResult`: ranked source lines with commit-pinned permalinks |
| `review`, `implement`, `ask`, `investigate`, `design`, `observe` | `EngineeringReport`: facts, findings, hypotheses, unknowns, limitations, truncations |

What was **not** read is part of the output. A ticket whose comment thread was
capped, a pull request whose file list was paginated to a ceiling, a file that
could not be decoded — each appears as a `Truncation` in the report rather than
being silently dropped.

---

## Quick start

### Python

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

`git` and `ripgrep` must be on `PATH`: local analysis, review and the sandbox
shell out to them. A missing binary is reported as a provider error rather than
treated as an empty result.

DevLens does **not** auto-load `.env`. Export variables in your shell, or point
`DEVLENS_CONFIG` at a TOML file.

Minimum for Jira + GitHub, single project:

```sh
export DEVLENS_JIRA_URL=https://your-team.atlassian.net
export DEVLENS_JIRA_EMAIL=you@example.com
export DEVLENS_JIRA_TOKEN=...
export DEVLENS_GITHUB_TOKEN=...
export DEVLENS_REPOSITORIES=your-org/your-repo
export DEVLENS_JIRA_PROJECTS=DEV

devlens ticket DEV-123 --repository your-org/your-repo --ref main
```

If a required variable is missing, DevLens names it:

```
devlens: invalid_input: DevLens is not configured. Set DEVLENS_JIRA_URL,
DEVLENS_JIRA_EMAIL, DEVLENS_JIRA_TOKEN, DEVLENS_GITHUB_TOKEN,
DEVLENS_REPOSITORIES, DEVLENS_JIRA_PROJECTS (or point DEVLENS_CONFIG at a TOML
file). The README lists what each one is for.
```

`analyze`, `ask`, `investigate`, `design` and `observe` need no credentials at
all; they run against a local checkout and say so in their limitations.

### API and UI

```sh
python -m pip install -e '.[dev]'
cd web && npm ci && npm run build && cd ..
uvicorn devlens.app.api:app --host 127.0.0.1 --port 8000
```

- App: <http://127.0.0.1:8000/>
- OpenAPI: <http://127.0.0.1:8000/docs>
- `/health` is liveness; `/ready` returns `503` with the reason when
  configuration is missing or invalid.

The built UI is discovered inside the installed package, so an installed wheel
serves it without further configuration. `DEVLENS_UI_DIR` overrides the location.

### UI development

```sh
cd web && npm ci && npm run dev
```

Vite serves <http://127.0.0.1:5173/> and proxies API routes to port 8000 (see
`web/vite.config.ts`).

---

## Docker

The image contains the backend, the compiled UI, `git` and `ripgrep`. Job
history persists in a volume.

```sh
cp .env.docker.example .env
docker compose up --build
```

The Compose service is deliberately constrained: read-only root filesystem, all
capabilities dropped, `no-new-privileges`, a PID ceiling, a memory limit, and a
size-limited tmpfs for `/tmp`. DevLens reads repositories and tickets that it
treats as hostile input, so the container it runs in is bounded rather than
trusted. The port is bound to `127.0.0.1`.

| Mount / setting | Purpose |
|---|---|
| `devlens-data` volume | SQLite job, run and approval stores |
| `./workspace` → `/workspace` (read-only) | Local checkouts for `analyze` and path-based modes |
| `DEVLENS_ANALYZE_ROOT=/workspace` | Confines every local path to that root |

Multi-project TOML:

```sh
cp devlens.example.toml devlens.toml
docker compose -f docker-compose.yml -f docker-compose.config.yml up --build
```

The Claude Code and Codex CLI binaries are **not** installed in the image. Use an
HTTP provider or an OAuth token.

---

## Configuration

Either single-project environment variables or a TOML file. Setting
`DEVLENS_CONFIG` replaces the single-project variables entirely.

| Variable | Purpose |
|---|---|
| `DEVLENS_JIRA_URL` | Jira Cloud site origin. Must be `https://<site>.atlassian.net` |
| `DEVLENS_JIRA_EMAIL`, `DEVLENS_JIRA_TOKEN` | Jira Cloud credentials |
| `DEVLENS_GITHUB_TOKEN` | GitHub token (default provider) |
| `DEVLENS_GIT_PROVIDER` | `github` (default) or `bitbucket_cloud` |
| `DEVLENS_BITBUCKET_EMAIL`, `DEVLENS_BITBUCKET_TOKEN` | Bitbucket Cloud credentials |
| `DEVLENS_REPOSITORIES` | Comma-separated `owner/repo` allowlist. Required |
| `DEVLENS_JIRA_PROJECTS` | Comma-separated Jira project keys. Required |
| `DEVLENS_CONFIG` | Path to a TOML file for multiple projects |
| `DEVLENS_UI_PASSWORD` | Enables the UI password gate |
| `DEVLENS_SESSION_SECONDS` | Session lifetime, 60–86400 (default 28800) |
| `DEVLENS_JOBS_DB` | SQLite path (default `.devlens-jobs.sqlite` in the working directory) |
| `DEVLENS_JOB_HISTORY` | How many finished jobs to keep (default 5000). `0` keeps every run, deliberately — a 75-minute soak of 270,000 runs put 1.5 GB in the database, so unbounded history is a choice rather than the default |
| `DEVLENS_ANALYZE_ROOT` | Root that local paths are confined to (default: the working directory) |
| `DEVLENS_UI_DIR` | Override the compiled UI location |
| `DEVLENS_AUDIT_LOG` | Path to an append-only JSONL audit trail |
| `DEVLENS_PUBLIC_ORIGIN` | Expected `Origin` for state-changing requests |
| `DEVLENS_SANDBOX_RUNTIME` | `podman`, `docker`, or `none` to disable container isolation |
| `DEVLENS_SANDBOX_IMAGE` | Image the runtime runs a repository's tests in (default `python:3.12-slim`) |
| `DEVLENS_ALLOW_ROOT_REPO_TESTS` | Permit repository code execution as root with no container runtime |

`DEVLENS_ANALYZE_ROOT` defaults to the working directory rather than to the whole
host. A path outside it is refused with a `403` naming the root. Widening it to
`/` is possible and explicit.

### Multi-project TOML

Copy `devlens.example.toml`. Credentials are referenced **by environment
variable name** — `jira_token_env`, `git_token_env`. A token written into the
file is rejected at startup, because a token in a config file is a token in
version control.

```sh
export DEVLENS_CONFIG="$PWD/devlens.toml"
export PAYMENTS_JIRA_TOKEN=...
export PAYMENTS_BITBUCKET_TOKEN=...
devlens ticket PAY-123 --project payments --repository your-workspace/payments
```

---

## Capabilities

Deny by default. Capability state is computed once from the environment at
startup and never from a provider response, so nothing DevLens reads can widen
its own permissions.

| Capability | What it unlocks | How to enable |
|---|---|---|
| `llm` | A model summarises retrieved evidence | `DEVLENS_LLM_PROVIDER` |
| `screenshot_analysis` | Image observations (never a root cause) | `DEVLENS_LLM_PROVIDER` + a vision-capable model |
| `observability_provider` | Loki, Prometheus, Tempo, SQL gateway | any of `DEVLENS_LOKI_URL` / `DEVLENS_PROM_URL` / `DEVLENS_TEMPO_URL` / `DEVLENS_SQL_URL` |
| `repository_code_execution` | Running a repository's own test suite | `DEVLENS_ALLOW_REPO_TESTS=1` |
| `git_writeback`, `jira_writeback` | Remote writes, after approval | `DEVLENS_ALLOW_WRITES=1` |
| `service_discovery` | **Not implemented.** Always denied | — |

`GET /capabilities` returns the enabled set, the denied set, the reason each one
is denied, and the variable that enables it. The UI takes its copy from there
rather than hard-coding a sentence.

---

## Language models (optional)

The model is never in the control path. It summarises evidence that has already
been retrieved; it cannot add facts, cannot state a root cause, and cannot
express a percentage. Retrieved content is wrapped in a non-forgeable
`<untrusted-data>` fence, and a source that writes the closing tag itself has it
neutralised rather than escaping into the instruction context.

**Selection is explicit.** `DEVLENS_LLM_PROVIDER` is the only thing that selects
a backend; a vendor key that happens to be in the environment is not a request to
use a model.

| `DEVLENS_LLM_PROVIDER` | Backend | Credential |
|---|---|---|
| `openai` | OpenAI-compatible chat API | `DEVLENS_LLM_API_KEY` |
| `claude` | Anthropic Messages API | `DEVLENS_ANTHROPIC_API_KEY`, or an OAuth session; endpoint `DEVLENS_ANTHROPIC_BASE_URL` |
| `codex` | Codex API | `DEVLENS_CODEX_API_KEY`, or an OAuth session; endpoints `DEVLENS_CODEX_BASE_URL` and `DEVLENS_CODEX_OAUTH_BASE_URL`, identified by `DEVLENS_CODEX_ORIGINATOR` |
| `claude_code` | Local `claude` CLI | the CLI's own login |
| `codex_cli` | Local `codex` CLI | the CLI's own login |

A base URL must be HTTPS unless it is loopback; otherwise an API key would travel
in clear text and DevLens refuses to start.

Usage is bounded per process: `DEVLENS_LLM_MAX_CALLS` (default 32) and
`DEVLENS_LLM_MAX_PROMPT_CHARS` (default 24000). `GET /settings` reports the
running totals.

### OAuth

DevLens reads a CLI credential file **only** when told to:

```sh
DEVLENS_LLM_OAUTH=1                       # allow the default cache locations
# or name the file explicitly:
DEVLENS_CLAUDE_CREDENTIALS=/path/to/credentials.json
DEVLENS_CODEX_AUTH=/path/to/auth.json
```

An expired Claude token is refreshed and written back in whichever unit the file
already used. Tokens can also be supplied directly with
`DEVLENS_CLAUDE_OAUTH_TOKEN` / `ANTHROPIC_AUTH_TOKEN` and
`DEVLENS_CODEX_OAUTH_TOKEN` / `CODEX_ACCESS_TOKEN`. A credentials file that
carries no client id of its own can be given one with
`DEVLENS_CLAUDE_OAUTH_CLIENT_ID`.

---

## Observability (optional)

| Variable | Backend |
|---|---|
| `DEVLENS_LOKI_URL`, `DEVLENS_LOKI_TOKEN` | Log search |
| `DEVLENS_PROM_URL`, `DEVLENS_PROM_TOKEN` | Metrics |
| `DEVLENS_TEMPO_URL`, `DEVLENS_TEMPO_TOKEN` | Traces |
| `DEVLENS_SQL_URL`, `DEVLENS_SQL_TOKEN` | SELECT-only queries through an HTTP gateway |

Each provider reports its own availability, and the report distinguishes three
outcomes that a list of strings cannot: the provider answered (`AVAILABLE`), the
provider answered with nothing (`EMPTY`), or the provider could not be reached
(`UNAVAILABLE` / `TIMEOUT` / `DENIED` / `NOT_CONFIGURED`). Only the first can
support anything. An empty result is recorded as "returned no matching data,
which is not evidence of absence".

SQL is parsed before it is sent: a single statement, beginning with `SELECT`, no
mutation keywords, no `INTO OUTFILE`, and only functions on a read-only
allowlist — `pg_read_file()` is a `SELECT` too. This is defence in depth. **The
gateway credential must also be a read-only role**; string parsing is not the
security boundary and DevLens does not claim it is.

---

## Write-back and approvals (optional)

```sh
export DEVLENS_ALLOW_WRITES=1
```

An approval authorises **one proposal**, not an action name. The proposal records
the exact payload, the run that produced it, and why; the approval records the
SHA-256 of that payload; at execution the hash is re-checked. An approved comment
cannot become a different comment.

| Permitted after approval | Always refused |
|---|---|
| `create_remote_branch` (under the `devlens/` prefix only) | `merge_pr` |
| `create_pr` | `deploy` |
| `post_pr_comment` | `release` |
| `post_jira_comment` | |

The three refusals cannot be enabled by any configuration, and they are checked
both in the request model and again at the write boundary.

Other properties the code enforces:

- Two concurrent approvals execute **once**. The pending → approving transition
  is a conditional `UPDATE`, so one caller claims the record and the other is
  told, before either reaches the network.
- Every approval expires (one hour by default). An expired approval cannot be
  executed.
- The repository or Jira project allowlist is re-checked at the write boundary
  rather than trusted from the read path.
- A write payload must carry the analysis. DevLens does not post placeholder text.

Without `DEVLENS_ALLOW_WRITES`, approving records the decision and says plainly
that nothing was sent to the remote.

---

## Executing repository code (optional)

```sh
export DEVLENS_ALLOW_REPO_TESTS=1
```

`review --run-tests` and `implement --run-tests` run the repository's own test
suite. That is arbitrary code from the repository under inspection, so:

- The child process receives an environment built from an **allowlist**
  (`PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TERM`, `SSL_CERT_FILE`), never a
  copy of DevLens's own. A new credential added to DevLens tomorrow is excluded
  by default rather than by remembering to add it to a denylist.
- Output is capped **as it is read**, not truncated afterwards, and the process
  group is killed on overflow or timeout — not just the direct child.
- CPU time, address space, file size and process count are limited with
  `setrlimit`.
- When `podman` or `docker` is available, the run is additionally confined with
  `--network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges
  --pids-limit`, and the workspace is mounted read-only.

Without a container runtime the run is still bounded by process limits — except
when DevLens is running as **root**, where they are not a bound at all:
`RLIMIT_NPROC` does not apply to uid 0, so a repository's own test suite could
fork without limit. DevLens refuses that combination rather than implying an
isolation it does not have:

```
Refusing to execute repository code as root with no container runtime: process
limits do not constrain a root user, so this would run unconfined. Install
podman or docker, run DevLens as a non-root user, or set
DEVLENS_ALLOW_ROOT_REPO_TESTS=1 if this host is disposable.
```

`DEVLENS_ALLOW_ROOT_REPO_TESTS=1` overrides it for a host you are willing to
lose, and the override is stated in every report's limitations. The image the
runtime uses is `DEVLENS_SANDBOX_IMAGE` (default `python:3.12-slim`).

---

## Operator UI

A hash-routed single-page application in `web/`, built to `web/dist` and served
by FastAPI at `/`. It is self-contained: no CDN, no external font, no analytics.
CI fails the build if the bundle references an external host.

Surfaces: Overview, Ask, Investigate, Reviews, Implement, Design, Observe,
Services, Repositories, Knowledge, Runs, Approvals, Integrations, Settings.

Accessibility properties the tests hold: status is never conveyed by colour
alone — severity, evidence status and confidence are separate components with
separate hue families and a text label each; dialogs are modal, labelled,
focus-trapped and closable with Escape; every icon-only control has an
accessible name; the current page is marked with `aria-current`.

Progress arrives over server-sent events as the work happens, resuming from a
sequence cursor, so a reconnect neither repeats nor drops an event.

The browser never receives a Jira or git token. `GET /settings` returns
configuration state, never a credential.

---

## CLI reference

```sh
devlens --help
```

| Command | Description |
|---|---|
| `ticket KEY --repository R [--project P] [--ref REF]` | Jira ticket against a repository |
| `analyze PATH [--query Q]` | Local git checkout only |
| `review [KEY] --repository R --pr N [--run-tests]` | Pull request heuristics |
| `implement KEY --repository R [--ref REF] [--run-tests]` | Change plan and proposals |
| `ask [QUESTION] [--path P] [--ticket T] [--repository R]` | Evidence-backed answer |
| `investigate [QUESTION] …` | Ranked hypotheses, each with its next experiment |
| `design [QUESTION]` | Design worksheet |
| `observe [--path P]` | Observability gap report |

Exit codes: `0` success, `1` a refusal or provider failure (with a one-line
reason, never a traceback), `2` a usage error.

---

## HTTP API

OpenAPI at `/docs`. Operator routes require a session when `DEVLENS_UI_PASSWORD`
is set.

Every failure carries a `kind` as well as a status, so a client can act on it:

| Kind | Status | Meaning |
|---|---|---|
| `policy_denied` | 403 | An allowlist or capability refused it |
| `conflict` | 409 | Already decided, expired, or no longer cancellable |
| `capacity` | 429 | Too many runs in flight; `Retry-After` is set |
| `invalid_input` | 422 | The request is malformed |
| `not_found` | 404 | No such job, approval or document |
| `provider_error` | 502 | An upstream system failed |
| `provider_unavailable` | 503 | A circuit is open |
| `timeout` | 504 | The run exceeded its budget |
| `internal_error` | 500 | Unexpected; the message is not echoed back |

### Health, auth and inventory

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/ready` | Ready, or 503 naming the configuration problem |
| GET | `/capabilities` | Enabled, denied, reason, and how to enable |
| GET | `/auth/status` | Whether a password is required |
| POST | `/auth/login`, `/auth/logout`, `/auth/refresh` | Session lifecycle |

### Analyses (synchronous) and jobs (asynchronous)

| Method | Path | Description |
|---|---|---|
| POST | `/analyses/{ticket,analyze,review,implement,ask,investigate,design,observe}` | Run and return the report |
| POST | `/jobs` | Enqueue (`202`); `idempotency_key` replays safely |
| GET | `/jobs`, `/jobs/{id}`, `/jobs/{id}/run` | Status, result, provenance |
| POST | `/jobs/{id}/cancel` | Cancel a queued or running job |
| GET | `/jobs/{id}/export` | JSON, or Markdown with `?format=md` |
| GET | `/jobs/{id}/events` | Server-sent events; send `Last-Event-ID` to resume |

`GET /jobs/{id}/run` returns the run's provenance: the DevLens and workflow
version, the capability set at run time, the model (if any), the repository and
commit, the requested ref, a configuration digest, and every tool call with its
duration and result size.

### Operator catalog

`/projects`, `/settings`, `/settings/{section}`, `/dashboard`, `/integrations`,
`/approvals`, `/proposals`, `/search`, `/notifications`, `/services`,
`/knowledge`, `/onboarding`, `/capacity`, `/audit`, `/intent`.

```sh
curl -sS -X POST http://127.0.0.1:8000/analyses/ticket \
  -H 'Content-Type: application/json' \
  -d '{"ticket_key":"DEV-123","repository":"your-org/your-repo","ref":"main"}'
```

---

## Multiple projects and Bitbucket Cloud

With `DEVLENS_CONFIG` set, each named project has its own Jira site, git
provider, repository allowlist and Jira project keys. One request analyses one
repository; an unknown project or repository returns `403`.

Bitbucket Cloud uses `workspace/repo_slug` and supports email plus API token
(Basic) or a bearer token. `HEAD` resolves the repository's actual default
branch rather than assuming `main`. Bitbucket Data Center is not supported.

---

## Hosted, multi-tenant mode

`devlens.app.api:app` is the single-operator application. When DevLens is
reachable by more than the person who started it, run
`devlens.app.saas:app` instead, with `DEVLENS_SAAS_CONFIG` pointing at a
hosting TOML file (see `devlens.hosting.example.toml`).

It adds: bearer or `__Host-` cookie identities with expiry, one isolated
application per tenant (its own job store, approval queue, audit log and
credentials), per-tenant request and concurrency quotas, an origin check on every
browser write, HTTPS enforcement, a 64 KiB body ceiling, and a structured access
log that records no bodies, credentials or query strings.

It also refuses, outright: remote writes, host paths, `implement`, and repository
code execution — because no isolated runner exists for those yet. A project
belongs to exactly one tenant, enforced at load.

---

## Architecture

A modular monolith. Python 3.11+, FastAPI, Pydantic v2, httpx, SQLite in WAL
mode; React 19 and Vite for the UI.

There is no planner, no tool-selection loop and no model in the control path.
DevLens runs fixed, auditable workflows — the class is called `WorkflowRunner`,
not an agent, because calling it an agent would overstate it.

- **Tool boundary** (`tools/context.py`): every call to the outside world checks
  the read policy and records a `ToolCall` before it touches the network, so a
  new workflow cannot reach a repository by forgetting a check.
- **Capability gate** (`guardrails.py`): one enforcement point per optional
  backend.
- **Policy** (`policies.py`): decisions over `(actor, action, resource,
  environment)` returned as a value that can be logged and tested.
- **Evidence** (`evidence.py`): provider results map to evidence statuses in one
  place; hypotheses are settled from evidence rather than asserted.
- **Sandbox** (`sandbox/workspace.py`): a disposable shallow clone with its
  `origin` remote removed after cloning, so the workspace cannot reach the
  network; paths are resolved before containment is checked, so a symlink cannot
  escape.
- **Resilience** (`resilience.py`): a rate limiter, circuit breaker and timeout
  per upstream host, so a Jira outage cannot stop DevLens reading GitHub.

The ticket workflow resolves a ref, lists the tree, ranks paths lexically, reads
the strongest matches under a cap, and links every excerpt to an immutable
commit. Lexical relevance is stated as a limitation on every such report: a term
match is not a root cause.

---

## Security

What this is, stated plainly.

**The local application** authenticates one operator with one shared password.
Sessions expire and rotate, and repeated failures back off per client. That is
adequate for one person on a loopback interface and is **not** an authentication
boundary for a shared network. Bind it to `127.0.0.1`. For anything wider, use
the hosted entry point.

**Untrusted input.** Jira descriptions, comments, attachments, commit messages,
pull request bodies, source code, logs and SQL values are all treated as data.
Instruction-like text is detected and flagged, never followed. Attachment
archives are checked for traversal and decompression ratio before extraction.

**Secrets.** Credentials are held as `SecretStr` and never appear in a repr, a
settings response, an audit record or a report. Redaction is recursive and
matches on key substrings — `token` catches `access_token` and `github_token`
without anyone remembering to add them — and URLs are redacted separately,
because credentials hide in userinfo and query strings where no key name appears.

**The audit trail** is an append-only file written under an exclusive lock, with
rotation at 8 MB. It is append-oriented; it is **not** tamper-proof. Anyone who
can write the file can rewrite it.

**Not redacted:** source excerpts in reports. DevLens quotes the repository you
pointed it at.

**Not provided:** SSO, roles beyond viewer/operator in hosted mode, per-user
audit in the local application, or any perimeter control. Configuration changes
require a restart.

---

## Development and tests

```sh
python -m pip install -e '.[dev]'

scripts/verify.sh           # every gate, in the order CI runs them
scripts/verify.sh --live    # also call the real GitHub API
scripts/verify.sh --fast    # skip the browser suite and the load tests
```

Individually:

```sh
ruff check .        # lint
mypy                # type check
pytest -q --cov=devlens --cov-report=term-missing

cd web && npm ci && npm run typecheck && npm run test && npm run build
cd web && npx playwright test    # real Chromium against a real DevLens process
```

The backend suite runs with every capability off and every credential scrubbed
from the environment, because that is the state the tool ships in. Provider
behaviour is exercised through HTTP transport fixtures, so no credential is
needed — with one deliberate exception: `tests/test_live_contract.py` calls the
real GitHub REST API unauthenticated, on one small public repository, and proves
the adapter parses what GitHub actually returns rather than what the fixture
says it returns. It skips with a stated reason when the API is unreachable or
the unauthenticated quota is spent; a skip is reported, never counted as a pass.

Beyond the unit suite: property-based tests check the honesty invariants against
generated input, load tests exercise the ceilings under real concurrency, and
`web/e2e/` drives a real Chromium against a real DevLens process — console
errors, off-origin requests, the event stream, the layout at phone width, and a
WCAG 2.1 AA audit of every surface in both themes.

Coverage is gated at the level the suite actually holds (see
`[tool.coverage.report] fail_under` in `pyproject.toml`). Raising the gate means
writing the tests that earn it, not editing the number.

CI runs lint, types, the Python suite on 3.11 and 3.12, the frontend type check,
tests and build, a check that the bundle loads nothing from a CDN, and
`scripts/smoke-container.sh` — which builds the image, starts it read-only with
every capability dropped, and then interrogates the running container: uid,
the compiled UI, an empty capability set, the security headers, a writable
`/data` over a read-only root, the binaries the workflows shell out to, and a
real run completing with no credentials. The browser suite is then run a second
time against that container, so the accessibility audit covers the bundle an
operator actually loads.

```sh
scripts/verify.sh              # every gate, in the order CI runs them
scripts/smoke-container.sh     # build the image and prove what it ships
```

Neither script is a summary of CI; CI invokes them, and
`tests/test_repository_hygiene.py` fails if the two ever disagree.

---

## Explicitly out of scope

- Automatic service discovery. The catalog is built from declarations and the
  repository allowlist; the capability is named `service_discovery` and is always
  denied.
- Numeric confidence scores.
- Merge, deploy or release, approved or otherwise.
- A root cause established from a screenshot.
- An in-process database for SQL; only `SELECT` through a configured gateway.
- Postgres, Redis, a task broker, or Kubernetes as a required runtime.
- Generating application code. `implement` produces a plan and proposals.

Provider references:
[Jira Cloud](https://developer.atlassian.com/cloud/jira/platform/rest/v3/),
[GitHub REST](https://docs.github.com/en/rest),
[Bitbucket Cloud REST](https://developer.atlassian.com/cloud/bitbucket/rest/).
