# DevLens — Full Repository, Architecture & Product Implementation Audit

**Repository:** `DevLens` @ `f51172d` (single commit, "Initialize DevLens async ticket analysis with multi-project GitHub and Bitbucket support")
**Audit date:** 2026-09-15
**Method:** full source read (6,679 LOC Python / 2,663 LOC web / 5,713 LOC tests), executed test suite, executed frontend build, executed TypeScript check, and 13 purpose-built behavioural probes run against the real code.
**Not executed:** Docker image build (no Docker daemon available in the audit environment). Everything attributed to Docker below is marked accordingly.

Every conclusion below is labelled **CONFIRMED / SUPPORTED / HYPOTHESIS / UNKNOWN / REJECTED** with confidence **Low / Medium / High / Very High**. Where a claim was reproduced by running code, the probe output is quoted.

---

## 1. Executive Summary

DevLens is a **small, disciplined, deterministic repository-and-ticket analysis tool** wrapped in a **much larger product narrative than the implementation supports**. The core `ticket` path — resolve ref → list tree → rank paths → read capped files → emit commit-pinned evidence with explicit limitations — is genuinely good work: honest, bounded, verifiable, and correctly SHA-pinned. Roughly a third of the repository is of that quality.

The rest divides into three categories:

1. **Deterministic scaffolding presented as investigation.** `investigate` always produces exactly one hard-coded hypothesis. `design` emits 24 literal `"<Section>: UNKNOWN — not enough evidence to specify."` lines. The `EngineeringAgent` has no loop, no planner, no tool selection and no LLM in its decision path — it is a dispatch table plus an audit call. "Agent", "workflow", "tool" and "timeline" all name things that are thinner than the words imply.

2. **One defect that inverts the product's central promise.** With observability providers configured but unreachable, DevLens records the connection errors themselves as `SUPPORTED` evidence, flips its hypothesis from `open` to `supported`, tells the operator it produced "hypotheses from code and live telemetry", and *removes* the honest "telemetry is unavailable" line from Unknowns. For a tool whose entire pitch is "evidence-first, no invented confidence", this is the most serious finding in the audit and it is reproducible in four lines of code.

3. **A sandbox that is a temporary directory.** `review --run-tests` and `implement --run-tests` execute `pytest` from a cloned repository with `os.environ.copy()` — the full DevLens process environment, including `DEVLENS_JIRA_TOKEN`, `DEVLENS_GITHUB_TOKEN` and `DEVLENS_ANTHROPIC_API_KEY`. A `conftest.py` in a target repository reads all of them at collection time. Reproduced end to end.

The documentation is, on balance, **more honest than most projects of this kind** — it explicitly disclaims the service catalog, code generation, merge/deploy and confidence percentages — but it also contains claims that are false today: the Docker image does not serve the UI, the documented test command does not pass, "SELECT-only" is weaker than it sounds, and "no LLM unless you configure one" is untrue if `ANTHROPIC_API_KEY` is already in your shell.

**Bottom line:** a good 3,000-line deterministic analyzer is currently wearing a 10,000-line platform costume. The foundation is worth keeping. The costume needs to come off before another feature is added.

---

## 2. Final Verdict

Answers to the seventeen required questions.

**1. Does DevLens currently work as documented?**
**Partially.** The `ticket`, `analyze`, `review` and `implement` paths behave as described. `investigate`, `design`, the SSE timeline, write-back content, the Docker UI, and the "deterministic unless you opt in" guarantee do not. Status: **CONFIRMED / High**.

**2. What percentage of documented capabilities are actually functional?**
**51%** — computed from the 40-row requirements matrix in §4 (COMPLETE = 1.0, PARTIAL = 0.5, STUB/BROKEN/MISSING = 0.0; sum 20.5 / 40 = 51.25%). The one capability documented as unavailable (`service_catalog`) is excluded because the documentation is accurate about it.

**3. Can I safely use it locally?**
**Yes, with two conditions:** never pass `--run-tests` against a repository you do not fully trust (DL-P0-001), and set `DEVLENS_ANALYZE_ROOT` (DL-P1-012). Read-only local use against your own repositories is safe.

**4. Can I safely expose the UI to an internal network?**
**No.** `DEVLENS_UI_PASSWORD` gives you: a single shared password, no rate limiting, no lockout, sessions that never expire, an in-memory session set, and a cookie without `Secure`. That is a speed bump, not an authentication boundary. The hosted entry point (`devlens.app.saas:app`) has real controls, but the README does not tell you to run it. **CONFIRMED / High.**

**5. Can it safely access private repositories?**
**For reading, yes.** Allowlists are enforced at the tool boundary (`ContextTools`) and again at the write boundary (`WriteGateway`), redirects are disabled, tokens never reach the browser, and SHAs are validated as 40-hex. **For cloning + executing, no** — see Q6.

**6. Can it safely execute repository code?**
**No.** There is no isolation of any kind: same user, same filesystem, same network, same environment, no resource limits. **CONFIRMED / Very High** (reproduced).

**7. Can it safely access production telemetry?**
**No — and not because of access control, because of evidence semantics.** DL-P0-002 means a failing production provider makes DevLens *more* confident rather than less. Connect telemetry only after that is fixed.

**8. Can write-back safely be enabled?**
**No.** The remote mutation itself is correctly fenced (merge/deploy denied, allowlists re-checked, unknown actions denied — all verified), but the *approval* is free text typed by whoever is signed in, is never tied to an agent proposal, is lost on restart, double-executes under concurrency, and the resulting comment says only `"DevLens review note."`. The gate is sound; nothing meaningful passes through it.

**9. Is deterministic mode genuinely useful without an LLM?**
**Yes for `ticket`, `analyze` and `review`. No for `investigate` and `design`.** The first three produce real, bounded, source-linked output with no model. The last two produce templates.

**10. Are Jira screenshot semantics correct?**
**The invariant is correct; the capability is a stub.** Image attachments yield PNG dimensions plus a canned `"UNKNOWN: screenshot alone cannot establish a root cause."`, and the screenshot→root-cause invariant is enforced structurally (no code path lets an image set `root_cause`). But vision is **never** applied to a Jira attachment — `infer_root_cause_from_image` is reachable only when `path` points at a local `.png`/`.jpg` on disk. The capability is also misnamed `screenshot_root_cause`, which describes the thing it is designed to prevent.

**11. Is the evidence/confidence model correctly enforced?**
**No.** Three separate failures: provider errors become `SUPPORTED` evidence (DL-P0-002); `Finding.confidence` is typed as an evidence status, so the documented Low/Medium/High/Very High band has no representation anywhere in the codebase; and `REJECTED` is documented as an evidence status but is absent from the `Confidence` literal, so a rejected hypothesis cannot be expressed.

**12. Is frontend behaviour genuinely connected to backend functionality?**
**Mostly yes for the run surfaces, no for the configuration surfaces.** Ask / Investigate / Review / Implement / Runs / Approvals / Knowledge / Services / Settings all make real calls and render real data. Integrations' connect wizard, the Repositories detail tabs, and the Design "Architecture" canvas are chrome.

**13. Which pages are real vs stubs?**
See §11. Functional: Overview, Ask, Investigations, Reviews, Implementations, Runs, Approvals, Knowledge, Onboarding, Services (list), Settings. Partial: Designs, Services (detail). Static/stub: Integrations wizard, Repositories detail, Architecture canvas, Landing.

**14. Top five engineering risks.**
1. Credential exfiltration via repository test execution (DL-P0-001).
2. Provider failure presented as supporting evidence (DL-P0-002).
3. Approval system that authorises nothing specific and double-fires (DL-P1-004/005).
4. "Deterministic by default" is not true when `ANTHROPIC_API_KEY` is in the environment (DL-P1-007).
5. No CI at all, and the documented test command fails (DL-P1-016).

**15. What should I fix before adding another feature?**
Phase A in §31: DL-P0-001, DL-P0-002, DL-P1-003, DL-P1-007, DL-P1-008, DL-P1-016. Six items, all small.

**16. What should I deliberately NOT build yet?**
Automatic service catalog discovery, multi-tenancy in the local app, a job queue, Postgres, code generation, a graph database, and any further UI surface. §28 and §33 explain why.

**17. Is the current architecture a good foundation for DevLens?**
**Yes, with one structural correction.** The modular monolith, the provider strategy/factory split, the deny-by-default capability facade, the policy-at-the-tool-boundary pattern and SQLite-for-one-node are all correct choices for this stage. The structural gap is that **`Run`, `Step`, `ToolCall`, `Evidence` and `Approval` are not first-class persisted objects** — they are fields on a report blob — and every reliability, reproducibility and approval defect in this audit traces back to that. Fix the domain model and the architecture is a good ten-year foundation.

---

## 3. Actual Architecture

Derived from the code, not from the README.

### 3.1 Runtime topology

```
┌─ Browser ────────────────────────────────────────────────────────────────┐
│  Vite/React 19 SPA, hash routing, no router lib, no state lib            │
│  fetch(credentials:"include")   EventSource(/jobs/{id}/events)           │
│  ⚠ Monaco loaded at runtime from cdn.jsdelivr.net (external)             │
└──────────────┬───────────────────────────────────────────────────────────┘
               │ same-origin HTTP, cookie session
┌──────────────▼───────────────────────────────────────────────────────────┐
│ FastAPI  devlens.app.api:app        (45 routes, 10 unauthenticated)      │
│   StaticFiles mount at "/"  ← resolves to <repo>/web/dist  [BROKEN in    │
│                                Docker: see DL-P1-003]                    │
│   AuthGate (in-memory session set)                                       │
├──────────────────────────────────────────────────────────────────────────┤
│ JobService — asyncio.Task per job, IN THE API EVENT LOOP                 │
│   • no queue, no worker, no restart recovery (local app)                 │
│   • JobStore → SQLite (journal_mode=delete, no index on events.job_id)   │
├──────────────────────────────────────────────────────────────────────────┤
│ ProjectAgents  {project → EngineeringAgent}                              │
│   Semaphore(max_analyses) + asyncio.timeout(analysis_timeout)            │
├──────────────────────────────────────────────────────────────────────────┤
│ EngineeringAgent  — NOT an agent: a dispatch table + AuditLog.record     │
│   ticket│review│implement│analyze│ask│investigate│design│observe         │
├──────────────────────────────────────────────────────────────────────────┤
│ Workflows (fixed, non-adaptive)        CapabilityGuard (deny-by-default) │
│   TicketWorkflow ─┐                      llm │ screenshot_root_cause     │
│   ReviewWorkflow ─┤                      observability_provider          │
│   ImplementWF    ─┼─► ContextTools ──►   git_writeback │ jira_writeback  │
│   AnalyzeWF      ─┤     ReadPolicy        service_catalog (always denied)│
│   Ask/Inv/Des/Obs ┘     Semaphore                                        │
├──────────────────────────────────────────────────────────────────────────┤
│ Providers                                                                │
│   JiraCloudProvider ──► https://*.atlassian.net   (httpx, redirects off) │
│   GitHubProvider    ──► api.github.com          ResiliencePolicy =       │
│   BitbucketProvider ──► api.bitbucket.org         RateLimiter +          │
│   WriteGateway      ──► same clients, re-checks allowlist   CircuitBreaker
│   ObserveHub        ──► Loki│Prometheus│Tempo│SQL  (own clients, NO      │
│   LlmProvider       ──► OpenAI│Anthropic│Codex│CLI  resilience policy)   │
│   RepoWorkspace     ──► local git + ripgrep subprocesses                 │
├──────────────────────────────────────────────────────────────────────────┤
│ "Sandbox" = tempfile.mkdtemp() + git clone --depth 50 + remote removal   │
│   ⚠ pytest/rspec run here with the FULL parent environment               │
└──────────────────────────────────────────────────────────────────────────┘

SEPARATE, UNDOCUMENTED ENTRY POINT:
  devlens.app.saas:app — multi-tenant ASGI gateway with per-tenant FastAPI
  children, HTTPS enforcement, __Host- cookies, session TTL, login quota,
  per-tenant SQLite, origin checks, security headers, job recovery.
  The README says multi-tenancy is "explicitly out of scope".
```

**Sync / async / persistent / external / optional markers**

| Element | Kind |
|---|---|
| `/analyses/*` | **sync** request-scoped, bounded by `analysis_timeout` (only when an agent exists) |
| `/jobs` | **async** in-process `asyncio.Task`, bounded by a *separate* hardcoded 120 s |
| `/jobs/{id}/events` | **async** 20 Hz SQLite poll; not a live event bus |
| `JobStore` | **persistent** SQLite |
| `ApprovalStore`, `KnowledgeStore`, `OnboardingStore`, `AuthGate.sessions`, `WriteGateway._seen` | **in-memory, lost on restart** (local app) |
| `AuditLog` | **persistent** only if `DEVLENS_AUDIT_LOG` is set; otherwise in-memory and unbounded |
| Jira / GitHub / Bitbucket / Loki / Prom / Tempo / SQL / LLM | **external** |
| LLM, observability, write-back, vision | **optional**, deny-by-default |

### 3.2 Data flow — an investigation request (§76)

```
Browser  POST /jobs {command:"investigate", question, ticket_key, path, project}
   │
   ├─ require_ui → AuthGate.check(cookie)                        [401 if required]
   ├─ request_payload(JobCreate) → PlatformRequest               [422 on bad shape]
   │     └─ authorize_analyze_path(path)                         [403 if outside root]
   ├─ JobStore.create → SQLite row status=queued                 [202 returned here]
   └─ asyncio.create_task(_run)
          │
          ├─ store.update(status=running); add_event(ToolStarted)   ← SSE event 1 of 2
          ├─ ProjectAgents.investigate → Semaphore → asyncio.timeout
          ├─ EngineeringAgent._traced("investigate") → AuditLog.record
          └─ InvestigateWorkflow.run
                ├─ _local_hits(path, query)
                │     open_local → RepoWorkspace.search → `rg` subprocess
                │     └─ EvidenceItem(type=code, confidence=SUPPORTED)
                ├─ ticket_key → EvidenceItem(type=jira, confidence=UNKNOWN)
                │     (no Jira API call is made on this path)
                ├─ _runtime(guard, query)
                │     └─ ObserveHub.collect → Loki / Prom / Tempo / SQL
                │          ⚠ failures are caught and returned AS ROWS
                │          ⚠ rows → EvidenceItem(confidence=SUPPORTED)   ← DL-P0-002
                ├─ Hypothesis H1 (hardcoded statement)
                │     status = supported iff any non-code SUPPORTED item ← DL-P0-002
                └─ EngineeringReport(evidence_items, hypotheses, timeline[4])
          │
          ├─ timeline events written to SQLite *after* completion   ← DL-P1-009
          ├─ store.update(status=completed, result=<JSON blob>)
          └─ add_event(RunCompleted)                                ← SSE event 2 of 2
   │
Browser  GET /jobs/{id}      (1 s poll)      → full record
         EventSource /events (20 Hz poll)    → 2 events, rendered as a live "Timeline"
         → Context pane | Hypotheses + Root cause | Evidence pane
```

### 3.3 What is missing from the topology

There is no queue, no worker process, no scheduler, no event bus, no run store, no evidence store, no approval store, no policy engine, and no isolation boundary. Each of those is a deliberate and defensible omission at this stage *except* the run/evidence store and the isolation boundary, which are load-bearing for the product's own claims.

---

## 4. Requirements / Implementation Matrix

Legend: **COMPLETE** (works as documented) · **PARTIAL** (works with undocumented gaps) · **STUB** (renders/returns but does nothing meaningful) · **BROKEN** (does not work) · **MISSING** · **NOT VERIFIED**.
Score: COMPLETE 1.0, PARTIAL 0.5, STUB/BROKEN/MISSING 0.0.

| # | Capability | Doc | BE | FE | Tests | E2E verified | Status | Score |
|---|---|---|---|---|---|---|---|---|
| 1 | Jira ticket analysis (`ticket`) | ✓ | ✓ | ✓ | ✓ | ✓ probe 11 | **COMPLETE** | 1.0 |
| 2 | Jira issue context (fields, links, subtasks, custom fields) | ✓ | ✓ | partial | ✓ | ✓ probe 9 | **PARTIAL** — comments truncated to the embedded page, no disclosure | 0.5 |
| 3 | Jira attachments | ✓ | ✓ | ✓ | ✓ | ✓ | **PARTIAL** — no count cap, sequential, PDF extraction is a regex | 0.5 |
| 4 | Screenshot analysis / vision | ✓ | stub | ✓ | ✓ | ✓ | **STUB** — never applied to Jira attachments | 0.0 |
| 5 | GitHub repository read (ref/tree/file) | ✓ | ✓ | n/a | ✓ | ✓ probe 11 | **COMPLETE** | 1.0 |
| 6 | GitHub pull request read | ✓ | ✓ | ✓ | ✓ | ✓ probe 9 | **PARTIAL** — first 100 files/commits only | 0.5 |
| 7 | GitHub write-back | ✓ | ✓ | partial | ✓ | ✓ probe 7 | **PARTIAL** — boilerplate content, `push_commit` misnamed | 0.5 |
| 8 | Bitbucket repository read | ✓ | ✓ | n/a | ✓ | ✓ | **PARTIAL** — one request per directory, 500-page cap | 0.5 |
| 9 | Bitbucket pull request read | ✓ | ✓ | ✓ | ✓ | ✓ probe 9 | **PARTIAL** — `next` link ignored | 0.5 |
| 10 | Bitbucket write-back | ✓ | ✓ | partial | ✓ | ✓ probe 7 | **PARTIAL** — as #7, plus hardcoded base `main` | 0.5 |
| 11 | Local repository analyze | ✓ | ✓ | ✓ | ✓ | ✓ probe 1 | **COMPLETE** | 1.0 |
| 12 | Review workflow | ✓ | ✓ | ✓ | ✓ | ✓ | **PARTIAL** — heuristics only (documented); 25 speculative reads | 0.5 |
| 13 | Implement workflow | ✓ | ✓ | ✓ | ✓ | ✓ | **PARTIAL** — plan only (documented); sandbox risk undocumented | 0.5 |
| 14 | Ask | ✓ | ✓ | ✓ | ✓ | ✓ probe 8 | **PARTIAL** — LLM affects only `executive_summary` | 0.5 |
| 15 | Investigate | ✓ | ✓ | ✓ | ✓ | ✓ probe 2 | **BROKEN** — one hardcoded hypothesis; evidence status inverted | 0.0 |
| 16 | Design | ✓ | template | ✓ | ✓ | ✓ probe 1 | **STUB** — 24 literal UNKNOWN lines | 0.0 |
| 17 | Observe | ✓ | ✓ | ✓ | ✓ | ✓ | **PARTIAL** — 80-file regex scan | 0.5 |
| 18 | Async jobs | ✓ | ✓ | ✓ | ✓ | ✓ probe 10 | **PARTIAL** — no restart recovery in the local app | 0.5 |
| 19 | SSE progress stream | ✓ | ✓ | ✓ | ✓ | ✓ probe 6 | **STUB** — start + end only; timeline replayed post-hoc | 0.0 |
| 20 | Approvals | ✓ | ✓ | ✓ | ✓ | ✓ probe 6/7 | **PARTIAL** — ephemeral, unlinked, races | 0.5 |
| 21 | Jira write-back | ✓ | ✓ | partial | ✓ | ✓ probe 7 | **PARTIAL** — fixed body text | 0.5 |
| 22 | Knowledge notes | ✓ | ✓ | ✓ | ✓ | ✓ | **PARTIAL** — persistent only in hosted mode | 0.5 |
| 23 | Services view | ✓ | ✓ | ✓ | ✓ | ✓ | **PARTIAL** — derived; contradicts `/capabilities` | 0.5 |
| 24 | Audit log | ✓ | ✓ | ✓ | ✓ | ✓ probe 10 | **PARTIAL** — exact-key redaction only, unbounded | 0.5 |
| 25 | LLM backends (5) | ✓ | ✓ | ✓ | ✓ | ✓ probe 2/8 | **PARTIAL** — implemented; used in exactly one place | 0.5 |
| 26 | Loki | ✓ | ✓ | ✓ | ✓ | ✓ probe 2 | **PARTIAL** — no time range, limit 20, failure = evidence | 0.5 |
| 27 | Prometheus | ✓ | ✓ | ✓ | ✓ | ✓ probe 2 | **PARTIAL** — instant query only | 0.5 |
| 28 | Tempo | ✓ | ✓ | ✓ | ✓ | ✓ probe 2 | **PARTIAL** — free-text `q`, 10 results | 0.5 |
| 29 | SQL gateway | ✓ | ✓ | ✓ | ✓ | ✓ probe 3 | **PARTIAL** — statement-shape check only; always `SELECT 1` in practice | 0.5 |
| 30 | Operator UI served by the Docker image | ✓ | ✗ | n/a | ✗ | ✓ reproduced | **BROKEN** | 0.0 |
| 31 | Multi-project isolation | ✓ | ✓ | ✓ | ✓ | ✓ probe 10 | **COMPLETE** | 1.0 |
| 32 | UI password auth | ✓ | ✓ | ✓ | ✓ | ✓ probe 6 | **PARTIAL** — no expiry, no rate limit, no `Secure` | 0.5 |
| 33 | Global search | ✓ | ✓ | ✓ | ✓ | ✓ | **PARTIAL** — substring match over last 100 jobs | 0.5 |
| 34 | Integrations connect wizard | ✓ | n/a | stub | ✗ | ✓ | **STUB** — 6 steps, no fields, no effect | 0.0 |
| 35 | Repositories detail page | ✓ | n/a | stub | ✗ | ✓ | **STUB** — all tabs `active`, no content | 0.0 |
| 36 | Settings sections | ✓ | ✓ | ✓ | ✓ | ✓ | **PARTIAL** — raw JSON dump; members/teams `unavailable` (documented) | 0.5 |
| 37 | Onboarding checklist | ✓ | ✓ | ✓ | ✓ | ✓ | **COMPLETE** | 1.0 |
| 38 | Capacity calculator | ✓ | ✓ | ✓ | ✓ | ✓ | **COMPLETE** | 1.0 |
| 39 | Export (JSON / Markdown) | ✓ | ✓ | ✓ | ✓ | ✓ | **COMPLETE** | 1.0 |
| 40 | Intent routing (`/intent`) | ✓ | ✓ | ✓ | ✓ | ✓ | **COMPLETE** | 1.0 |
| — | Service catalog auto-discovery | documented as **not implemented** | — | — | — | — | **MISSING (honestly documented)** | excluded |

**Computation:** 8 rows × 1.0 + 25 rows × 0.5 + 7 rows × 0.0 = **20.5 / 40 = 51.25%**.

---

## 5. End-to-End Flow Verification

### 5.1 Scenario 1 — Ticket (§69). **CONFIRMED / Very High** (executed, probe 11)

```
POST /analyses/ticket {ticket_key:"DEV-123", repository:"org/repo", ref:"main"}
  → AnalysisRequest (pydantic: key ^[A-Z][A-Z0-9_]*-[1-9]\d*$, repo owner/name, ref ≤200 chars)
  → require_ui  → ProjectAgents._run("default","analyze_ticket")
  → Semaphore(max_analyses=8) + asyncio.timeout(120)
  → EngineeringAgent._traced → AuditLog.record
  → TicketWorkflow.run
      ContextTools.ticket → ReadPolicy.authorize(repo ∧ jira project)
        → GET /rest/api/3/issue/DEV-123?fields=summary,description     [1 call]
      build_context
        → GET /repos/org/repo/commits/main                             → sha f×40
        → GET /repos/org/repo/git/trees/<sha>?recursive=1              [1 call]
        → filter: 13 extensions, exclude node_modules/vendor/dist/build/.*, 0 < size ≤ 100 000
        → rank by |terms(path) ∩ terms(summary+description)|, tie → alphabetical
        → TaskGroup reads top 20 files concurrently                    [20 calls]
        → per-line lexical match → Evidence(path, line, excerpt≤400, url, matched_terms)
        → sort by |matched_terms|, cap 20
  → AnalysisResult
```

Observed output:
```
status: completed | provider: github | commit: ffffffffffffffffffffffffffffffffffffffff
summary: Found 20 relevant source lines for DEV-123.
evidence[0]: src/checkout/handler_0.py:2 terms=['checkout','instagram','reply','timeout']
  url=https://github.com/org/repo/blob/ffff…ffff/src/checkout/handler_0.py#L2
limitations:
  - Lexical relevance only; findings do not establish a root cause.
  - Only eligible text files are inspected; hidden, generated, and oversized files are excluded.
  - Inspected the top 20 of 41 eligible files.
  - No language model is used.  (+4 standard limitations)
Jira API calls: 1 | GitHub API calls: 22
```
**Verdict: correct, bounded, honestly limited, and genuinely commit-pinned.** This is the best-engineered path in the repository.

### 5.2 Scenario 2 — Review (§70). **CONFIRMED / High**

```
PR metadata + diff + commits (3 calls) → optional Jira issue context (1 call)
→ read up to 15 changed files + guessed sibling test paths, capped at 25 reads
     (every failure swallowed by `except Exception: continue`)
→ review_changes(diff, files, contents)
     added-line regex: secrets / AWS keys / private keys / eval|exec|system|popen / bare except
     whole-file AST (py) or line heuristic (other): query-call-inside-loop → N+1 HYPOTHESIS
     missing_tests: new .py/.rb with no sibling test → P3
→ optional `--run-tests`: clone → pytest/rspec → append stdout tail  ⚠ DL-P0-001
→ EngineeringReport(findings, diff[:200 000], pull_request, ticket)
→ Review UI: file list, Monaco diff, findings filtered by selected file
```
**Do findings reference changed code?** Yes for the regex findings — they are anchored to `(path, line)` parsed from the diff's added lines. **No for the N+1 findings** — those run over the *whole file at head*, so a finding can point at code the PR never touched. Undisclosed. **CONFIRMED / High.**

### 5.3 Scenario 3 — Investigation (§71). **BROKEN — CONFIRMED / Very High**

```
question → _local_hits(path, query)        [ripgrep, 1 evidence item if any hit]
        → ticket key captured as an evidence item (no Jira call is made)
        → _runtime(guard, query) → ObserveHub.collect
        → ONE hardcoded Hypothesis H1, always the same sentence
        → RunCompleted
```
Executed with three providers configured and pointed at a closed port:
```
[SUPPORTED] log    Loki         Logs: Provider could not be reached.
[SUPPORTED] metric Prometheus   Metrics: Provider could not be reached.
[UNKNOWN  ] trace  Tempo        Traces UNAVAILABLE: provider is not configured.
[SUPPORTED] sql    PostgreSQL   Database: Provider could not be reached.
hypothesis status -> supported
executive_summary -> "Investigation produced hypotheses from code and live telemetry."
unknowns -> ['Screenshot vision cannot confirm a root cause by itself.']
```
Three outage messages became supporting evidence, promoted the hypothesis, and displaced the honest unknown. See DL-P0-002.

### 5.4 Scenario 4 — Implement (§72). **CONFIRMED / High**

Boundary is explicit and correctly stated: **DevLens does not generate application code.** It produces `"Inspect <path> for <problem>."` for each evidence path, creates a local `devlens/<TICKET>` branch inside a disposable clone, optionally runs the repo's tests, and discards the clone. The report says so in `executive_summary`, `limitations` and `implementation_plan`. No remote is touched. The only defect on this path is the sandbox (DL-P0-001).

### 5.5 Scenario 5 — Approval (§73). **CONFIRMED / Very High**

```
UI "Create pull request" → POST /approvals {action:"create_pr", target:"<repo>"}
                           (free text; no proposal, no run id, no payload, no expiry)
       ↓ in-memory list
operator clicks Approve → POST /approvals/{id}/approve
       ↓ reads pending.status == "pending"        ← TOCTOU window opens
       ↓ await writes.execute(action, target)     ← yields
       ↓ WriteGateway: allowlist re-check ✓  HUMAN_ONLY ✓  allowed ✓
       ↓ _seen[key] set only AFTER the remote call returns
       ↓ store.decide(approved)                   ← TOCTOU window closes
```
Two concurrent approvals of one record: **2 remote executions**, both responses `approved`.
```
gateway executions for one approval id: 2
```
No audit entry is written for the approval or the write — `AuditLog.record` is only called from `EngineeringAgent._traced`.

### 5.6 Scenario 6 — Partial failure (§74). **CONFIRMED / High**

Jira ✓, git ✓, LLM ✓, Prometheus down, SQL timing out. `ObserveHub.collect` catches per provider, so the run **completes** rather than failing — the isolation behaviour is correct and is what you want. But the result is wrong in the other direction: the two failures are reported as `SUPPORTED` evidence rather than as `UNAVAILABLE`, and the summary claims live telemetry. The report retains successful evidence (good) and misstates the limitations (bad).

---

## 6. Critical Findings — P0

### DL-P0-001 — Repository test execution inherits every DevLens secret

| | |
|---|---|
| **Severity** | P0 |
| **Confidence** | CONFIRMED / Very High (reproduced end to end) |
| **Category** | Security — sandbox isolation, credential exposure |
| **Affected** | `src/devlens/tools/shell.py:69`, `agent/workflows/review.py:119-141`, `agent/workflows/implementation.py:64-75`, `sandbox/workspace.py` |

**What happens.** `run()` builds the child environment as `merged = os.environ.copy()`. `review --run-tests` and `implement --run-tests` clone an allowlisted repository into `tempfile.mkdtemp()` and execute `pytest -q` (or `bundle exec rspec`) inside it with that environment.

**Why it happens.** The module is named `sandbox` but implements no sandbox: no environment scrubbing, no user separation, no namespace, no cgroup, no network policy, no filesystem restriction, no CPU/memory/pid/disk limit. `Workspace` is a directory that gets `rmtree`'d.

**Production scenario.** A contributor adds `conftest.py` to a repository already on `DEVLENS_REPOSITORIES` (an insider, a compromised account, or a fork merged without review). pytest imports `conftest.py` during *collection*, before any test runs.

**Evidence.** Reproduced:
```python
# conftest.py placed in the cloned repo
pathlib.Path('/tmp/exfil.txt').write_text(repr(
    {k:v for k,v in os.environ.items() if 'TOKEN' in k or 'API_KEY' in k}))
```
```
pytest returncode: 0
EXFILTRATED BY REPOSITORY CODE -> {'DEVLENS_JIRA_TOKEN': 'jira-secret-123',
 'DEVLENS_GITHUB_TOKEN': 'ghp_secret_456', 'DEVLENS_ANTHROPIC_API_KEY': 'sk-ant-secret-789', …}
```
`markers()` returns `pytest: True` if merely `pyproject.toml` exists, so virtually every Python repository triggers this path.

**Impact.** Full compromise of every credential DevLens holds — Jira, GitHub/Bitbucket, LLM, observability, SQL gateway — plus arbitrary code execution as the DevLens user with its network access and filesystem. In Docker the blast radius stops at the container; run outside Docker and it is the operator's whole machine.

**Recommendation.**
1. Immediately: pass an explicit minimal environment — `env={"PATH":…, "HOME":<sandbox>, "GIT_TERMINAL_PROMPT":"0", "PYTHONDONTWRITEBYTECODE":"1"}` — built by allowlist, never `os.environ.copy()`. Add `-p no:cacheprovider`. This is a five-line change.
2. Add `resource.setrlimit` (RLIMIT_AS, RLIMIT_NPROC, RLIMIT_FSIZE, RLIMIT_CPU) via `preexec_fn`, and always kill the process group on timeout.
3. Gate `--run-tests` behind a new deny-by-default capability `repository_code_execution`, separate from read.
4. Before untrusted repositories: a real boundary (container with `--network=none --read-only --cap-drop=ALL --pids-limit`, or a microVM).
5. Rename `sandbox/` to `workspace/` until (4) exists; the current name promises a guarantee the code does not provide.

**Verification.** A test that plants a `conftest.py` writing `os.environ` to a file and asserts no `DEVLENS_*`, `ANTHROPIC_*`, `GITHUB_*` key is present.

**When to fix.** **Now**, before any further use of `--run-tests`. Hard blocker before executing code from any repository that is not fully trusted.

---

### DL-P0-002 — Unreachable observability providers are recorded as SUPPORTED evidence and promote the hypothesis

| | |
|---|---|
| **Severity** | P0 |
| **Confidence** | CONFIRMED / Very High (reproduced) |
| **Category** | Correctness — evidence integrity (the product's central claim) |
| **Affected** | `providers/observe.py:100-119`, `agent/workflows/platform.py:85-109, 188-222` |

**What happens.**
```python
# observe.py collect()
except (ProviderError, AccessDenied) as exc:
    collected[name] = [str(exc)]          # ← the error becomes a data row
```
```python
# platform.py _runtime()
rows = collected.get(kind)
if rows is None: … UNAVAILABLE …
items.append(_item(kind, provider, f"{label}: {'; '.join(rows[:3])}", "SUPPORTED"))
```
A failed call is not `None`, so it takes the success branch and is stamped `SUPPORTED`. `InvestigateWorkflow` then does:
```python
status="supported" if any(i.confidence == "SUPPORTED" and i.type != "code" for i in items) else "open"
```
and selects the summary and unknowns purely on `"observability_provider" in guard.enabled` — configuration, not outcome.

**Why it happens.** There is no `ProviderStatus` in the domain model. Availability, emptiness and content are all collapsed into `list[str]`. The same defect makes `"Loki returned no matching log lines."` — the literal no-results sentinel — indistinguishable from a log line, so **absence of evidence is also recorded as SUPPORTED evidence**.

**Production scenario.** Prometheus is behind an expired certificate during an incident. An engineer runs an investigation. DevLens reports "Investigation produced hypotheses from code and live telemetry", shows three blue SUPPORTED evidence cards, marks H1 supported (which the UI renders as confidence band **"High"** via `hypothesisBand`), and drops "Logs, metrics, traces, and database access are not configured" from Unknowns.

**Evidence.** Reproduced with all three URLs pointed at `127.0.0.1:9`:
```
[SUPPORTED] log    Loki         Logs: Provider could not be reached.
[SUPPORTED] metric Prometheus   Metrics: Provider could not be reached.
[SUPPORTED] sql    PostgreSQL   Database: Provider could not be reached.
hypothesis status -> supported
executive_summary -> "Investigation produced hypotheses from code and live telemetry."
unknowns -> ['Screenshot vision cannot confirm a root cause by itself.']
```

**Impact.** Inverts the single guarantee DevLens sells. An outage in the systems being investigated makes DevLens *more* confident. Any engineer who trusts the confidence chip is being misled precisely when the stakes are highest. This also silently poisons `/dashboard`, `/search`, `/notifications` and the services catalog, which all read from stored job results.

**Recommendation.**
1. Introduce `ProviderResult = {status: AVAILABLE|UNAVAILABLE|DENIED|TIMEOUT, rows: list, error: str|None, query: str, window: (t0,t1)}` and return it from every `ObserveHub` method.
2. In `_runtime`, map `AVAILABLE ∧ rows` → `SUPPORTED`; `AVAILABLE ∧ ¬rows` → `UNKNOWN` with observation `"no matching data"`; everything else → `UNKNOWN` with `UNAVAILABLE: <reason>`.
3. Derive `executive_summary` and `unknowns` from the observed statuses, never from `guard.enabled`.
4. Add the invariant as code: an `EvidenceItem` may not be `SUPPORTED` unless it carries a non-empty `metadata["rows"]` and `metadata["provider_status"] == "AVAILABLE"`. Assert it in a model validator so no future workflow can bypass it.

**Verification.** Regression test: configure a provider at a closed port, assert every resulting item is `UNKNOWN`, the hypothesis stays `open`, and `unknowns` contains the telemetry caveat.

**When to fix.** **Now.** Hard blocker before connecting DevLens to production telemetry.

---

## 7. High Findings — P1

### DL-P1-003 — The Docker image never serves the operator UI
**CONFIRMED / Very High** (reproduced by replicating the install mode; the image itself was NOT built — no Docker daemon available).
`app/api.py:585` computes `_UI = Path(__file__).resolve().parents[3] / "web" / "dist"`, which assumes an editable install from the repository root. The Dockerfile does `pip install --no-cache-dir .` (non-editable) and copies the build to `/app/web/dist`.
```
api file: /tmp/dlprod/lib/python3.11/site-packages/devlens/app/api.py
_UI:      /tmp/dlprod/lib/python3.11/web/dist
_UI exists: False
mounts: []
```
`mount_ui` returns `False`, no `StaticFiles` mount is created, and `GET /` returns 404. The README's "Open http://127.0.0.1:8000/" and "The image includes … production web build" are both false for the documented Docker path. **Fix:** ship `web/dist` as package data (`[tool.setuptools.package-data]`) and resolve it via `importlib.resources`, or set `DEVLENS_UI_DIR=/app/web/dist` in the Dockerfile and honour it in `mount_ui`. Add a smoke test asserting `GET /` is 200 in the built image. **When:** now — it is a one-line env var away.

### DL-P1-004 — Concurrent approvals execute the same remote write twice
**CONFIRMED / Very High** (reproduced). `api.py:334-347` reads `pending.status == "pending"`, `await`s the remote call, then calls `decide()`. `WriteGateway._seen[key]` is populated only *after* the await returns, so it cannot dedupe concurrent callers. Measured: one approval id → 2 gateway executions; one `create_pr` target → 2 remote PR creations. **Fix:** transition the record to `approving` under a per-record `asyncio.Lock` *before* the await; make `_seen` a reservation taken before the call; key idempotency on the approval id, not `action:target`. **When:** before enabling write-back.

### DL-P1-005 — Approvals authorise nothing specific
**CONFIRMED / High.** `ApprovalCreate` is `{action: str(1..80), target: str(1..400)}` — arbitrary text from any signed-in operator, with no link to a run, no payload, no expiry, no actor, and no audit entry. No workflow ever creates one (verified by grep: `ApprovalStore` appears only in `api.py` and `saas.py`). The queue accepts `merge_pr` and `deploy` records and surfaces them in `/notifications` as "DevLens wants to deploy org/repo" even though nothing generated them. `WriteGateway` correctly refuses to execute them, so the *mutation* is safe; the *authorisation record* is meaningless. **Fix:** an approval must reference `{run_id, proposal_id, action, resource, payload_hash, requested_by, expires_at}`; workflows emit proposals; `/approvals` accepts only an existing proposal id; re-validate the payload hash at execution; persist the record; write an audit entry at request, decision and execution. **When:** before enabling write-back.

### DL-P1-006 — Write-back posts fixed boilerplate; `push_commit` pushes nothing
**CONFIRMED / Very High** (reproduced). Real gateway, writes enabled:
```
('create_branch','org/repo','devlens/x')
('create_branch','org/repo','devlens/repo')      ← this was action "push_commit"
('create_pr','org/repo','DevLens devlens/change','devlens/change','main')
('pr_comment','org/repo',7,'DevLens review note.')
('jira_comment','DEV-1','DevLens note for DEV-1.')
```
`push_commit` calls `create_branch` at HEAD — it creates a ref and pushes no commit. PR title/head/base are hardcoded (`DevLens devlens/change` → `main`) with an empty body. Comments carry none of the analysis. The feature the README calls "operator-approved Jira/git write-back" produces content-free remote mutations. **Fix:** either carry the report into the payload (comment body = rendered findings, PR body = plan) or remove the actions until they do. Rename `push_commit` → `create_remote_branch`. **When:** before enabling write-back.

### DL-P1-007 — A stray `ANTHROPIC_API_KEY` silently enables the LLM and vision
**CONFIRMED / Very High** (reproduced). `llm_oauth.claude_api_key()` reads `ANTHROPIC_API_KEY`; `claude_oauth_token()` reads `ANTHROPIC_AUTH_TOKEN`; `codex_oauth_token()` reads `CODEX_ACCESS_TOKEN` — all generic, widely-exported variables.
```
DEVLENS_LLM_PROVIDER unset, ANTHROPIC_API_KEY set ->
  {'enabled': ['llm','screenshot_root_cause'], 'denied': [...]}
```
The README says "Leave unset for fully deterministic behavior" and "If DEVLENS_LLM_PROVIDER is omitted, auto-selection uses keys only". Any engineer with a Claude key in their shell gets a DevLens that calls an external model, changes its reported limitations, and can read a local `.png` and ship it off-box (`platform.py:195-205`). **Fix:** auto-selection reads only `DEVLENS_*` variables; generic vendor variables require `DEVLENS_LLM_PROVIDER` to be set explicitly. **When:** now.

### DL-P1-008 — Jobs stay `running` forever after a restart (local app)
**CONFIRMED / Very High** (reproduced). `JobStore.recover_interrupted()` exists and is correct, and is called **only** from `saas.py:102`. The documented entry point never calls it.
```
status after restart, no recovery call: running
status if recover_interrupted() were called: failed
```
With `restart: unless-stopped` in Compose, every crash leaves permanently "running" rows. The UI polls them forever at 1 s and `EventSource` streams at 20 Hz indefinitely. **Fix:** call it in `api.lifespan` startup. One line. **When:** now.

### DL-P1-009 — The SSE stream is not live progress
**CONFIRMED / Very High** (reproduced). Only two real events are ever emitted: `ToolStarted` at the start, `RunCompleted` at the end. Workflow `timeline` events are written to the store *after* the run completes (`jobs.py:338-345`), i.e. replayed, not streamed.
```
data: {"kind":"ToolStarted","message":"running design"}
data: {"kind":"RunCompleted","message":"completed"}
```
The Investigation workspace renders this under a **"Timeline"** heading with the placeholder *"Waiting for the first engineering action…"*, and the UI simultaneously polls `/jobs/{id}` every second — so the SSE channel adds load and the appearance of live activity without the substance. This is the "fake activity indicator" pattern. **Fix:** either emit events from inside the workflows as work happens (pass a callback into `ContextTools`) or remove the stream and label the panel "Run log". **When:** before the next UI feature.

### DL-P1-010 — Silent first-page-only pagination
**CONFIRMED / Very High** (reproduced).

| Source | Fetched | Disclosed? |
|---|---|---|
| Jira comments | the embedded page only (20 of a `total` of 137 in the probe); `total` ignored | **no** |
| GitHub PR files | first 100; `Link: rel="next"` never read | yes, only when `len == 100` |
| GitHub PR commits | first 100 | **no** |
| Bitbucket PR files | first 100; `next` ignored | yes, only when `len == 100` |
| Bitbucket PR commits | first page | **no** |

For an evidence-first tool, `spec.important_comments = comments[-5:]` over an arbitrary first page is the worst case: it looks like a selection and is an accident of pagination. **Fix:** follow `next`/`Link` with a page cap; when the cap is hit, record it in `limitations` *and* in the object, so the report states what it did not see. **When:** before trusting review or ticket output on large PRs and busy tickets.

### DL-P1-011 — `run()` buffers unbounded subprocess output and orphans the child on timeout
**CONFIRMED / Very High** (measured). `ripgrep` on a tree with vendored/generated files emits 19.4 MB in **0.056 s** standalone. Through `asyncio.wait_for(process.communicate(), 60)` the same call takes **60.07 s** and hits the timeout; the `[:20_000]` cap is applied only after the whole buffer is read. On timeout `wait_for` cancels the read but never calls `terminate()`/`kill()`, so the child is orphaned.
```
direct:    rg -n --max-count 50 -- token  →  0.056 s, 19 427 774 bytes
via run(): 60.07 s → ProviderError("Command exceeded its time budget."), child not reaped
```
`POST /analyses/analyze` on such a repository took **60.64 s** in the audit environment. On the unconfigured path there is no request timeout at all. `RepoWorkspace.tree()` and `markers()` also perform synchronous `rglob`/`glob` inside `async def`, blocking the event loop. **Fix:** stream stdout with an incremental byte cap and kill the process group when it is exceeded or the deadline passes; add `--glob` exclusions and `--max-filesize` to the ripgrep call; move blocking filesystem walks to `asyncio.to_thread`. **When:** before exposing analyze to anyone but yourself.

### DL-P1-012 — Default deployment is unauthenticated and unrestricted
**CONFIRMED / Very High** (reproduced). With no environment at all, every operator route probed answered 200 (only `/ready` returned 503), and:
```
POST /analyses/analyze {"path":"/home/claude/devlens"} → 200
  status: completed | files listed: 50
  facts[0]: "Workspace /home/claude/devlens contains 2000 listed files."
```
`authorize_analyze_path` returns immediately when `DEVLENS_ANALYZE_ROOT` is unset, which is the default. Anyone who can reach the port reads any git repository on the host: tree, log, HEAD commit, working-tree diff (4 000 chars), status, ripgrep hits, blame, README/ARCHITECTURE contents. The README does say "treat the API as a local operator interface" and Compose binds `127.0.0.1`, which is why this is P1 and not P0 — but *secure by default* would be the opposite: fail closed unless a root is configured. **Fix:** default `DEVLENS_ANALYZE_ROOT` to the process CWD and require an explicit opt-out; make `/analyses/analyze` refuse when no root is set. **When:** now.

### DL-P1-013 — "SELECT-only" blocks statement shape, not side effects
**CONFIRMED / Very High** (reproduced). `select_only()` correctly rejects multiple statements, CTE-wrapped writes, comment tricks and mutation keywords. It allows:
```
ALLOWED  'SELECT pg_sleep(30)'
ALLOWED  "SELECT lo_import('/etc/passwd')"
ALLOWED  "SELECT pg_read_file('/etc/passwd')"
ALLOWED  'SELECT * FROM pg_shadow'
ALLOWED  "SELECT xp_cmdshell('whoami')"
```
DevLens is also the *only* line of defence documented — the README never says the gateway credential must be read-only. Separately, `collect()` substitutes `"SELECT 1"` whenever the user's text is not already a SELECT, so in practice the SQL provider is a liveness probe whose result is filed as `SUPPORTED` "Database" evidence. **Fix:** (a) document and require a read-only role with file-read, large-object and `dblink` privileges revoked; (b) keep the parser as defence in depth but deny function calls outside an allowlist; (c) stop fabricating `SELECT 1` — if there is no query, report `UNKNOWN`. **When:** before connecting a real database.

### DL-P1-014 — Circuit breaker and rate limiter are shared across unrelated providers; timeouts never trip the breaker
**CONFIRMED / High.** `dependencies.py:93-123` builds one `ResiliencePolicy` per project and `attach`es the same instance to both the Jira client and the git client. A Jira outage opens the circuit for GitHub and vice versa — the opposite of a bulkhead. Additionally, `ResiliencePolicy.call` wraps `breaker.call` in `asyncio.timeout`; a timeout raises `CancelledError` (a `BaseException` in 3.11+), which `except Exception` inside `CircuitBreaker.call` does not catch, so `_on_failure()` never runs and **timeouts never contribute to opening the circuit**. `ObserveHub` and `LlmProvider` clients have no resilience policy at all. **Fix:** one policy per upstream host; catch `BaseException` (or explicitly `asyncio.CancelledError`) in the breaker; attach a policy to observability and LLM clients. **When:** before production telemetry.

### DL-P1-015 — Severity and evidence status are conflated in the model and in the palette
**CONFIRMED / Very High.**
- `domain.py:5` — `Confidence = Literal["CONFIRMED","SUPPORTED","HYPOTHESIS","UNKNOWN"]`. **`REJECTED` is missing**, although the README lists it as an evidence status. A rejected hypothesis cannot be expressed on a `Finding` or an `EvidenceItem`.
- `Finding.confidence: Confidence` — so the field named *confidence* holds an *evidence status*. The documented Low/Medium/High/Very High band exists nowhere in Python; it appears only as a UI-side mapping (`Catalog.tsx:344 hypothesisBand`) that invents `confirmed→"Very High"`, `supported→"High"`, else `"Medium"` — a UI-invented calibration, exactly what the README promises not to do.
- `styles.css` — `--p3: #3b82f6` is the same token value as `--info`, used for `.conf-SUPPORTED`; `--p0: #ef4444` equals `--err`, used for `.conf-REJECTED`. A P3 severity chip and a SUPPORTED status chip are the same blue.
- `Severity` defaults to `"P3"`; the domain uses P1–P4; the CSS styles P0–P3; `.sev-P4` does not exist. The Review header counts P0 (never produced) and ignores P4 (producible).

**Fix:** three distinct types — `Severity = P0..P3`, `EvidenceStatus = CONFIRMED|SUPPORTED|HYPOTHESIS|UNKNOWN|REJECTED`, `ConfidenceBand = LOW|MEDIUM|HIGH|VERY_HIGH` — with three non-overlapping colour ramps, and delete `hypothesisBand`. **When:** now; it gets more expensive with every consumer.

### DL-P1-016 — No CI, and the documented test command fails
**CONFIRMED / Very High** (executed). There is no `.github/`, no CI configuration of any kind, no lint configuration in `pyproject.toml` (despite a `.ruff_cache/` in the tree), and no type-checker configuration. The README's own command:
```
python -m pytest -q --cov=devlens --cov-fail-under=100
→ 175 passed in 10.09s
→ TOTAL 3807 stmts 1128 branch 99% — FAIL Required test coverage of 100% not reached. Total coverage: 99.96%
```
Two partial branches in `resilience.py` (`54->59`, `75->exit`). The command documented as the project's quality gate exits non-zero on a clean checkout. `npm run build` is `vite build` alone — no `tsc`, so TypeScript errors cannot fail a build (`tsc --noEmit` is currently clean, but nothing enforces that). **Fix:** add a workflow running pytest + coverage + ruff + `tsc --noEmit` + `vite build` + `docker build` + a `GET /` smoke test against the image; either close the two branches or set the threshold to what the suite actually achieves. **When:** now — everything else in this report regresses silently without it.

### DL-P1-017 — Monaco is fetched from a third-party CDN at runtime
**CONFIRMED / Very High** (found in the built bundle). `@monaco-editor/react` loads the editor from `https://cdn.jsdelivr.net/npm/monaco-editor@0.55.1/min/vs` at runtime. The operator UI therefore executes third-party script from a public CDN and **the Review diff viewer does not work offline or on an air-gapped network** — neither is mentioned anywhere. `package.json` pins `monaco-editor ^0.56.0` locally, which is never used and is a *different version* from the one loaded. **Fix:** `loader.config({ paths: { vs: '/assets/vs' }})` and copy the local package into `dist` at build time; or drop Monaco for a lightweight diff renderer (it is used in exactly two read-only places). **When:** before any internal-network deployment.

---

## 8. Medium Findings — P2

**DL-P2-018 — `policies.require()` is dead code.** CONFIRMED/Very High. `AUTO_PERMISSIONS` / `DENIED_PERMISSIONS` (23 operations including `production_shell`, `db_mutation`, `install_dependency`) and `require()` are never called from `src/` — only imported for `ReadPolicy`. A reader auditing DevLens sees an operation-level permission model that does not exist. Delete it or wire it into `ContextTools`.

**DL-P2-019 — Audit redaction is exact-key-name only.** CONFIRMED/Very High (reproduced):
```
{"token":"***", "Authorization":"***", "access_token":"A", "github_token":"G",
 "headers":{"authorization":"***","x-api-key":"K"},
 "nested":[{"password":"***","refresh_token":"R"}],
 "url":"https://u:p@host/x?token=leak"}
```
`access_token`, `github_token`, `refresh_token`, `x-api-key` and URL credentials survive. Recursion is correct; the matcher is not. Today only `_traced` payloads are recorded (which contain no secrets), so exposure is latent — but the README promises "no secrets are written". Use substring matching on `token|secret|password|key|auth|cookie|credential` plus URL redaction. Also: `AuditLog.recent()` does `path.read_text()` on the whole file, and the in-memory list is unbounded — both grow without limit.

**DL-P2-020 — Local session management.** CONFIRMED/Very High (reproduced). Cookie: `HttpOnly; Path=/; SameSite=strict` — **no `Secure`, no `Max-Age`**. Sessions live in a `set()` that never expires and never evicts. Over 30 consecutive wrong passwords produced a 403 each time with no delay, lockout or quota. A single shared password with no identity. The hosted app gets all of this right (`__Host-` prefix, `secure=True`, TTL, 1 000-session cap, 20/min login quota) — port those controls down.

**DL-P2-021 — Bitbucket tree walk amplification.** CONFIRMED/High. `list_files` BFS-walks one HTTP request per directory (GitHub does it in one recursive call), capped at `MAX_PAGES=500`. A 1 000-directory repository costs 1 000 sequential requests or fails outright. The pagination-safety checks (cycle detection, host/path pinning on `next`) are genuinely good; the traversal strategy is not. Prefer a metadata listing with a depth parameter, or accept and document a depth limit.

**DL-P2-022 — Lexical ranking is weak in specific, predictable ways.** CONFIRMED/High (reproduced):
```
terms('AccountResolver') ∩ terms('account_resolver.py') → set()
terms('P95 latency') → ['latency']          # P95 dropped (needs 4+ chars after a letter)
candidates.sort(key=lambda f: (-len(terms(f.path) & keywords), f.path))
```
Ranking uses **path tokens only** — file content never influences which files are read — and ties break alphabetically, so "top 20 of 41" is arbitrary whenever the tie set is large (observed: `handler_0, handler_1, handler_10, …`). camelCase in a ticket never matches snake_case on disk. **Fix:** split camelCase/PascalCase on boundaries, keep alphanumeric tokens like `p95`/`http2`, and break ties by path depth and recency (`git log` order) rather than alphabet.

**DL-P2-023 — Argument-injection surface in the shell allowlist.** CONFIRMED/High (reproduced). `allowed()` returns `True` for `['git','-c',"core.pager=sh -c 'id'",'log']`, `['git','clone','--upload-pack=…','--','u','d']`, `['git','log','--output=…']` and `['pytest','--co','-p','evil']`. The `-c` skip loop strips config pairs and then checks only the verb. Separately, the user-controlled `ref` (pattern-free, ≤200 chars) reaches `git checkout --detach <ref>` and `git rev-parse <ref>` **without a `--` separator**. No current call site is attacker-controlled in a way that reaches RCE, so this is P2 — but the allowlist provides far less protection than it appears to. **Fix:** reject any argv element beginning with `-` after the verb unless it is on a per-verb allowlist; always insert `--` before user values.

**DL-P2-024 — Unbounded Jira attachment fan-out.** CONFIRMED/High. `for item in fields.get("attachment") or []` has no count cap; each attachment is downloaded sequentially at up to 2 MB. A 50-attachment ticket is 50 serial requests and up to 100 MB held in memory, inside a request that has no attachment-specific budget. Cap the count, run them through the existing semaphore, and record what was skipped.

**DL-P2-025 — Review performs 25 speculative reads and swallows every error.** CONFIRMED/High. `related_paths()` guesses five test-file names per changed file; the loop is `except Exception: continue`. For a 15-file PR that is ~25 GitHub requests of which most 404, and a genuine auth/rate-limit failure is indistinguishable from "file absent". Use the tree listing you already fetch to test existence, and narrow the except clause.

**DL-P2-026 — Three UI surfaces are non-functional chrome.** CONFIRMED/Very High.
- `Integrations.tsx:68-86` — a six-step "Connect" wizard whose only behaviour is `setStep(step+1)`. No fields, no test, no persistence. It implies DevLens can connect integrations from the UI; it cannot (everything is environment-driven).
- `Catalog.tsx:219-223` — Repositories detail renders six tabs as `<span className="tab active">`; all are simultaneously "active" and none is clickable.
- `ArchitectureCanvas.tsx` — a hardcoded `External API → Collector → Queue → Service/DB` graph, rendered under the Design workspace's **"Architecture"** tab for every design regardless of input. That is a fabricated architecture diagram presented as output.

**DL-P2-027 — The UI states capability facts as hardcoded strings.** CONFIRMED/High. `Support.tsx:216` "Approved writes still cannot push, comment, or open a PR." and `Implement.tsx:57,154` "Writes stay behind approval and remain denied" are false when `DEVLENS_ALLOW_WRITES=1`. `Investigation.tsx:349` "Vision root-cause is disabled" is false when an LLM is configured. `Support.tsx:181,194` hardcode "tokens — · cost —" and "Tokens and cost stay empty while language models are disabled". `/capabilities` already returns the truth; render from it.

**DL-P2-028 — The secure entry point is the undocumented one.** CONFIRMED/Very High. `saas.py` implements HTTPS enforcement, `__Host-` cookies with TTL, login quota, per-tenant request quota and in-flight caps, origin checks on writes, 64 KiB body limit, `nosniff`/`no-store`/`referrer-policy`/HSTS headers, structured request logging that deliberately excludes bodies and credentials, single-worker file locking, per-tenant SQLite, and job recovery — while `api.py`, the entry point the README tells you to run, has none of it. The README lists multi-tenancy as out of scope. Either document `saas.py` as the network-facing deployment, or backport its controls to `api.py`.

**DL-P2-029 — Two unrelated timeout layers.** CONFIRMED/High. `JobService(JobStore())` in `api.lifespan` uses the hardcoded default `timeout=120.0`; `Settings.analysis_timeout` is applied only inside `ProjectAgents._run`. Configuring `analysis_timeout = 600` in TOML does not change job behaviour in the local app (the hosted app passes it correctly). `/analyses/analyze` on the no-agent path has no timeout at all.

**DL-P2-030 — SQLite configuration is wrong for the access pattern.** CONFIRMED/Very High (measured): `journal_mode=delete` (not WAL), `timeout=2`, connection opened/closed per operation, **no index on `events.job_id`** — while `/jobs/{id}/events` runs `SELECT * FROM events WHERE job_id=?` every 50 ms per open stream. Three concurrent investigations plus their event streams is ~60 full scans per second against a writer-locked database with a 2 s busy timeout. `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, an index on `events(job_id, ts)` and a longer poll interval are all trivial and together move the ceiling by an order of magnitude.

**DL-P2-031 — "Agent" overstates the execution model.** CONFIRMED/Very High. `EngineeringAgent` has no loop, no planner, no tool selection, no state, no retry and no LLM in its control path — `_traced` calls a fixed workflow and records a timing. The workflows are straight-line functions. `ContextTools` is a policy-checked provider facade, not a tool registry. The LLM is consulted in exactly one place (`AskWorkflow`, for one string). Nothing here is wrong as *engineering* — a deterministic pipeline is the right thing to build first — but the naming makes the system harder to reason about and sets false expectations. Rename to `WorkflowRunner` / `ProviderFacade`, or build the execution model the names describe.

**DL-P2-032 — Dead code.** CONFIRMED/Very High. `LocalGitProvider` (120 lines) is referenced only by tests; production `analyze` uses `RepoWorkspace` directly. `ContextTools.post_jira_comment / create_pull_request / push_commit / resolve_service / search_logs / complete / infer_root_cause_from_image` are never called by any workflow. `Router` is used only by the CLI and tests. Plus `policies.require` (DL-P2-018). Roughly 250 lines of plausible-looking but inert code — all of it in security-relevant modules, which is where dead code does the most damage to a reader's model of the system.

**DL-P2-033 — `/services` contradicts `/capabilities`.** CONFIRMED/Very High. `GET /capabilities` reports `service_catalog` denied and `guardrails.BOUNDARIES` says "Service catalog auto-discovery is not implemented", while `GET /services` returns a derived catalog and the UI ships a Services section with nine tabs. The *implementation* is defensible (a catalog derived from the repository allowlist is exactly the right first step, see §32); the *inventory* is wrong. Also: `BOUNDARIES["observability_provider"] = "Observability providers are not implemented."` — they are; the message is stale.

**DL-P2-034 — Light-theme contrast failures.** CONFIRMED/High. `[data-theme="light"]` overrides only background/text tokens; the severity and status colours are unchanged. `--p2: #eab308` on `#ffffff` is ≈1.9:1 and `--ok: #22c55e` is ≈2.2:1, both far below the 4.5:1 WCAG AA threshold for the 14 px chip text. Define a second ramp under `[data-theme="light"]`.

**DL-P2-035 — Modal semantics and focus management.** CONFIRMED/High. The command palette, search overlay, attachment viewer and integrations wizard are `<div className="palette" role="presentation">` with no `role="dialog"`, no `aria-modal`, no focus trap and no focus restoration. `Escape` is wired globally for the palette and search but not for the attachment viewer or the wizard. Async panels have no `aria-live`, so screen-reader users get no announcement when a run completes. **What is already right:** `:focus-visible` is styled globally, `prefers-reduced-motion` is honoured, and every chip renders its text label next to the dot — so status is *not* colour-dependent, which is the requirement that matters most.

---

## 9. Low Findings — P3

- **DL-P3-036** `npm run build` does not typecheck (`vite build` only). `tsc --noEmit` is currently clean — keep it that way by adding it to the script. CONFIRMED/Very High.
- **DL-P3-037** `monaco-editor` is a declared dependency that is never bundled; the CDN serves a different version (0.55.1 vs the pinned ^0.56.0). CONFIRMED/Very High.
- **DL-P3-038** Tests write durable fixtures into `tests/.scratch/` inside the repository (23 directories present), and `test_project_agents_review_implement` depends on the process CWD being a git repository — it fails in any checkout without `.git`. Use `tmp_path`. CONFIRMED/Very High (reproduced).
- **DL-P3-039** `test_coverage_fill.py` (732 lines), `test_last_gaps.py` (331), `test_unit_gaps.py` (382) — 1 445 lines, a quarter of the suite, named after a coverage metric rather than a behaviour. See §23. CONFIRMED/Very High.
- **DL-P3-040** `AnalysisResult.method` is `Literal["lexical"]` — a one-value enum that cannot express any future method. SUPPORTED/High.
- **DL-P3-041** Unhandled promise rejections: `KnowledgePage`, `OnboardingPage`, `RunsPage`, `DesignWorkspace`, `SearchOverlay` all call `api.get(...).then(...)` with no `.catch`. `App.tsx:108` falls back to `authenticated: true` when `/auth/status` fails. CONFIRMED/High.
- **DL-P3-042** `_nested_query_findings` calls `ast.get_source_segment` for every descendant of every `for` node — quadratic on large files, and `get_source_segment` re-splits the source each time. CONFIRMED/High.
- **DL-P3-043** `App.tsx:134-136` calls `go("/overview")` during render. `parse_added_lines` increments the line counter on `\ No newline at end of file` markers. `AuthGate.login` raises `TypeError` (→ HTTP 500) on a non-ASCII password. SUPPORTED/Medium.

---

## 10. Backend Review

**Is it modular, or just foldered?** Genuinely modular in the parts that matter. Evidence: the dependency direction is clean and acyclic — `domain` ← `policies`/`guardrails` ← `providers` ← `tools` ← `workflows` ← `agent` ← `app`. `domain.py` imports nothing but pydantic. Provider adapters are addressed through `GitProvider`/`JiraProvider` protocols and constructed by a strategy table (`GIT_STRATEGIES`), so adding GitLab is a table entry plus an adapter. Authorization sits at one boundary (`ContextTools`) rather than being sprinkled across routes — exactly the pattern §83 of the brief asks for. `WriteGateway` re-checks it independently, which is correct defence in depth. No circular imports, no global mutable state beyond `app.state`, no business logic in route handlers.

**Where it is not modular.** Three places:
1. `app/api.py` (596 lines, 45 routes) is a god module: routing, auth, job orchestration, approval execution, integration health, project listing, error translation and static mounting. The `_jobs`/`_approvals`/`_knowledge`/`_onboarding`/`_auth`/`_guard` lazy-init helpers duplicate the same six-line pattern and exist only because `lifespan` may not have run.
2. Capability enforcement is split across `CapabilityGuard`, `WriteGateway`, `approvals.WRITE_ACTIONS` and `policies.DENIED_PERMISSIONS` — four tables with overlapping and inconsistent contents (`WRITE_ACTIONS` maps `merge_pr → git_writeback`, implying it could ever be allowed; `HUMAN_ONLY` correctly denies it; `DENIED_PERMISSIONS` lists it again and is never consulted).
3. `agent/workflows/platform.py` mixes four unrelated workflows, the evidence factory, the unavailability table and the design section list in one 299-line module.

**Error handling.** `_translate` maps `AccessDenied→403`, `ProviderError→502`, `TimeoutError→504` and re-raises everything else to a 500. `ProviderError` messages are deliberately sanitised at the provider boundary ("Provider request failed (HTTP 404)"), so upstream detail does not leak — good. `JobService._execute` stores only `type(exc).__name__` for unexpected exceptions, which is safe but means an operator debugging a failure gets the word `KeyError` and nothing else. The taxonomy is coarse: see §20.

**Resilience.** Token-bucket limiter and three-state breaker are correctly implemented in isolation; their wiring is wrong (DL-P1-014). There are **no retries anywhere** — which, given the write-back idempotency situation, is the safer default and worth keeping until approvals are properly keyed.

**Type safety.** Pydantic v2 throughout with tight field patterns (ticket key, repository, project name) — the strongest input-validation story in the codebase. Gaps: `ref` has no pattern (DL-P2-023); `Settings` does not set `extra="forbid"`, so a typo in `devlens.toml` is silently ignored (`HostingConfig` does set it — the right pattern is already in the repo); `EvidenceItem.metadata: dict` and `JobRecord.result: dict` are untyped escape hatches; `_item(kind, …)` carries `# type: ignore[arg-type]`; no mypy/pyright configuration exists so none of this is checked.

---

## 11. Frontend Review

**Stack.** React 19 + Vite 6, ~2 200 LOC across 15 files. No router (hash parsing in `api.ts:route`), no state library, no data-fetching library, no component library, hand-written CSS with custom properties. For an application this size that is a defensible, even admirable, set of choices — there is no framework tax and the whole thing is readable in an afternoon.

**Page classification (§11 of the brief).**

| Page | Status | Basis |
|---|---|---|
| Overview | **FUNCTIONAL** | `/dashboard`, `/intent`, real routing, real findings roll-up |
| Ask | **FUNCTIONAL** | submits a job, polls, renders question→sources→reasoning→answer |
| Investigations (list) | **FUNCTIONAL** | real list, real filters |
| Investigations (workspace) | **PARTIAL** | 3-pane, evidence and hypotheses are real; "Timeline" is post-hoc (DL-P1-009); "Root cause" always reads *Not confirmed* because no workflow ever sets `root_cause`; Services/Repositories/Attachments tabs are one line each |
| Reviews | **FUNCTIONAL** | file list, Monaco diff, findings, severity counts, MD export. Selecting a file filters findings but not the diff |
| Implementations | **FUNCTIONAL** | plan, files, verification, risks, stage rail, approval request |
| Designs | **PARTIAL** | 10 tabs; Capacity is real and honest ("Calculated locally. Not invented by a model."); Architecture is a fixed fake (DL-P2-026); the other 8 substring-filter the 24 UNKNOWN facts |
| Observe | **FUNCTIONAL** | submits, redirects to the investigation workspace |
| Runs | **FUNCTIONAL** | list + raw JSON artifact dump; tokens/cost hardcoded to "—" |
| Approvals | **FUNCTIONAL** | lists, approves, rejects; the explanatory copy is wrong (DL-P2-027) |
| Services (list) | **FUNCTIONAL** | real `/services` |
| Services (detail) | **PARTIAL** | 9 tabs, 7 of which render the same disclaimer |
| Repositories (list) | **FUNCTIONAL** | from `/projects` |
| Repositories (detail) | **STATIC/STUB** | all tabs `active`, no content |
| Knowledge | **FUNCTIONAL** | list, filter, create, detail (in-memory server-side) |
| Integrations | **PARTIAL** | status grid is real; the connect wizard is a stub |
| Settings | **PARTIAL** | eight real sections rendered as a raw JSON `<pre>` |
| Onboarding | **FUNCTIONAL** | real steps, real completion inference |
| Landing | **STATIC** | marketing copy + ASCII diagram |

**Component and state issues.** `Catalog.tsx` (374 lines) holds seven unrelated page components — Landing, Onboarding, Services, Knowledge, Repositories, SettingsHub, SearchOverlay, DesignWorkspace, `hypothesisBand` and `RunsTable`. `Support.tsx` (260) similarly holds Ask, Designs, Runs, Approvals and Observe. Neither name describes its contents. Polling is duplicated verbatim in four components (`InvestigationWorkspace`, `AskResult`, `ReviewWorkspace`, `ImplementWorkspace`) — the same `let timer; const load = () => …setTimeout(load, 1000)` block — and should be one `useJob(jobId)` hook. `projects` is prop-drilled from `App` through every page. Cleanup is actually correct everywhere (`clearTimeout` + `source.close()` in every return), which is better than most React code of this size. Two real race conditions: `ReviewStart`/`ImplementStart` initialise `repository` from `projects[0]` captured at first render, so the field stays empty when `/projects` resolves after mount; and `InvestigationWorkspace` runs SSE *and* 1 s polling simultaneously, so `job` and `events` can disagree.

**Design system.** Coherent: a full token set (indigo scale, four surface levels, four semantic colours, two fonts, one rail width), a light theme, a consistent `.card`/`.chip`/`.tab`/`.field` vocabulary, `:focus-visible` styling and `prefers-reduced-motion`. Two real defects: the severity and evidence-status ramps share token *values* (DL-P1-015) and the light theme does not re-derive them (DL-P2-034).

---

## 12. Frontend ↔ Backend Contract Review

Every frontend call was traced to its route. **No mismatched or broken contracts were found** — the API client and the pydantic models agree, and `Job`, `Project`, `Approval`, `EvidenceItem`, `Hypothesis`, `Finding`, `ServiceItem`, `KnowledgeDoc` and `Capacity` in `api.ts` are faithful mirrors of the server models. The defects are semantic, not structural:

| UI action | Request | Route → layer | Response → state | Verdict |
|---|---|---|---|---|
| Sign in | `POST /auth/login` | `AuthGate.login` | cookie + `setAuth` | ✓ (cookie flags, DL-P2-020) |
| Route universal input | `POST /intent` | `detect_intent` | `intentPath` → hash | ✓ |
| Start investigation | `POST /jobs {command:"investigate"}` | `request_payload`→`JobService`→`InvestigateWorkflow` | 202 → `/investigations/{id}` | ✓ contract; ✗ semantics (DL-P0-002) |
| Live progress | `EventSource /jobs/{id}/events` | SQLite poll | `events[]` → "Timeline" | ✗ **not live** (DL-P1-009) |
| Cancel | `POST /jobs/{id}/cancel` | `JobService.cancel` | replaces `job` | ✓; non-cancellable returns **502**, should be 409 |
| Open run | `GET /jobs/{id}` | `JobStore.get` | full record | ✓ |
| Export | `GET /jobs/{id}/export?format=md` | `to_markdown` | new tab | ✓ |
| Run review | `POST /jobs {command:"review"}` | `ReviewWorkflow` | findings + diff | ✓ |
| Request PR approval | `POST /approvals {action:"create_pr",target:repo}` | `ApprovalStore.create` | success toast | ✗ toast text is wrong (DL-P2-027); target is free text (DL-P1-005) |
| Approve / reject | `POST /approvals/{id}/(approve\|reject)` | `WriteGateway` | refresh | ✗ races (DL-P1-004) |
| Load integrations | `GET /integrations` | env presence | status grid | ✓ — but "healthy" means *a variable is set*, not that a connection works |
| Global search | `GET /search?q=` | substring | grouped results | ✓ |
| Settings | `GET /settings/{section}` | `settings_section` | `<pre>` JSON | ✓ |

One structural gap: **`POST /approvals` exists with no UI other than the Implement page's button**, and no workflow ever calls it — the queue has no producer.

---

## 13. Agent / Workflow Review

**What "agent" means here: a dispatch table.** `EngineeringAgent.__init__` constructs eight workflow objects; each public method calls `_traced(command, request, workflow.run)`; `_traced` times the call, merges `guard.limitations()` into the result and hands the event to `AuditLog`. There is no loop, no planner, no tool selection, no memory, no retry, no self-correction, and no LLM anywhere in the control path.

**Is deterministic mode truly independent of the LLM?** **Yes — verifiably.** `CapabilityGuard._need("llm")` raises unless enabled; `LlmProvider.from_env()` returns `None` when no key is present; the CLI's platform commands construct a bare `CapabilityGuard()` so they can *never* reach a model. Executed with a clean environment, all four platform workflows returned 200 with real deterministic content and no outbound model call. The single caveat is DL-P1-007: "no key present" is defined too loosely.

**Can providers influence policy?** **No.** Capability state is computed once from the environment at startup; no provider response feeds back into `guard.enabled`. This is the right design and it is correctly implemented.

**Are tools and workflows explicit?** Workflows yes — eight named classes with a single `run`. Tools no — `ContextTools` is a fixed method surface, not a registry; there is no `ToolCall` object, no per-call record, no arguments captured, and no way to enumerate what a run did. `EngineeringReport.timeline` is the nearest thing and carries at most five hand-written events.

**Verdict.** This is **architecture that does not pretend to be agentic in its code, but does in its vocabulary**. The code is honest; the names are not. That matters because the names are what the README, the UI and future contributors reason about.

---

## 14. Evidence & Confidence Model Review

**Domain concepts — presence, persistence, lifecycle.**

| Concept | Exists? | Where | Persisted? | Lifecycle | Verdict |
|---|---|---|---|---|---|
| Run | ~ | `JobRecord` | ✓ SQLite | queued→running→completed\|failed\|cancelled | **conflated with Job**; sync `/analyses/*` runs have no Run at all |
| Workflow | ✓ | classes | ✗ | none | no version, no identity in the output |
| Step | ✗ | — | — | — | **implicit** |
| ToolCall | ✗ | — | — | — | **implicit** — nothing records which provider call happened with what arguments |
| Observation | ~ | `EngineeringReport.observations: list[str]` | as JSON blob | none | **conflated with Evidence** |
| Evidence | ✓✓ | two unrelated types: `Evidence` (path/line/url) and `EvidenceItem` (type/source/observation/confidence) | as JSON blob | none | **two models for one concept** |
| Hypothesis | ✓ | `Hypothesis` | as JSON blob | open→supported→rejected→confirmed | modelled well, produced trivially (always one, hardcoded) |
| Finding | ✓ | `Finding` | as JSON blob | none | no id, so it cannot be referenced, deduplicated or tracked |
| Artifact | ✗ | — | — | — | missing |
| Approval | ✓ | `ApprovalRecord` | **✗ in-memory** | pending→approved\|rejected | no link to a Run or a proposal |
| Capability | ✓ | `CapabilityGuard` | env | static | good |
| Policy | ~ | `ReadPolicy` | config | static | resource allowlist only; no actor/action/environment |
| Provider | ✓ | protocols + strategy table | config | — | good |
| Project | ✓ | `ProjectSettings` | config | — | good |
| Repository | ~ | a `str` | — | — | not a first-class object |

**The concepts that should become explicit, in priority order:** `ProviderStatus` (fixes DL-P0-002), `ToolCall` (fixes reproducibility and the fake timeline in one move), `Proposal` (fixes the approval model), `Run` distinct from `Job` (a job is a scheduling artifact; a run is an investigation).

**Observation vs Evidence (§34).** Currently conflated: `EvidenceItem.observation` is a free string and `EngineeringReport.observations` is an unrelated list of strings. The distinction the brief proposes is worth adopting *because it dissolves DL-P0-002*: an Observation is something DevLens noticed (always valid, no status); Evidence is verifiable source material with provenance and a status. "Prometheus could not be reached" is then trivially an Observation and cannot be Evidence, because Evidence requires a successful retrieval. That is a genuine architectural payoff, not a naming exercise.

**Immutability and provenance (§33).** Evidence is a plain mutable pydantic model stored inside a JSON blob; it can be edited by any code holding the report before persistence, and there is no hash, no signature and no "as-of" timestamp. `Evidence` (lexical) has good provenance — repository, commit SHA, path, line, matched terms. `EvidenceItem` (platform) has almost none — `source` is a free string and `metadata` is empty everywhere it is constructed.

**Reproducibility (§39).** Captured today: repository, resolved commit SHA, project, provider, ticket key, PR head SHA (review only). **Missing:** DevLens version, workflow version, requested ref (only the resolved SHA survives), Jira snapshot time and issue version, telemetry time range, model provider/name, configuration digest, capability set at run time, tool-call log. `AnalysisResult` exposes 10 fields, none of them temporal. A report cannot currently be used to explain, six weeks later, why DevLens said what it said.

**Confidence integrity (§37).** **Good news first:** there are no fake percentages anywhere. A full-repository search for `%`, `probability`, `score` and numeric confidence found nothing — the README's central promise is kept at the arithmetic level, and `capacity()` is explicitly labelled as locally calculated. The failures are structural, not numeric: DL-P0-002 (status assigned from configuration rather than outcome), DL-P1-015 (severity/status/band conflation, missing `REJECTED`), and `hypothesisBand` inventing a band client-side.

**Screenshot safety (§38).** The invariant *"screenshot evidence alone cannot produce a CONFIRMED root cause"* is **structurally enforced**, which is better than prompt wording: `process_attachment` hardcodes `"UNKNOWN: screenshot alone cannot establish a root cause."` and never sets a confidence; `observe_image` caps every line at `SUPPORTED:` and always appends the UNKNOWN caveat; `root_cause` is never assigned by any workflow. Two corrections needed: rename the capability `screenshot_root_cause` → `screenshot_analysis` (it currently names the thing it forbids), and either wire vision to Jira attachments or stop implying it exists.

---

## 15. Jira Review

**Trace.** `Settings.from_env` validates the site URL hard (HTTPS, `*.atlassian.net`, no userinfo/query/fragment/path) → Basic auth `(email, token)` on a per-project `httpx.AsyncClient` with redirects disabled → `ReadPolicy.authorize_ticket` (project-key prefix must be allowlisted) → `GET /rest/api/3/issue/{key}?fields=…` → ADF flattening → comments → attachments → `build_ticket_spec`.

**Good.** The URL validator is unusually strict and correct. The ADF walker handles `hardBreak`, `mention`, `emoji`, `inlineCard`, `codeBlock`, tables and list items rather than naively concatenating `text`. `_custom_fields` handles the three shapes Jira actually returns. `_allowed_url` pins attachment downloads to the Jira host, and the download then goes through the API path by attachment id, so SSRF is not reachable. `process_attachment` computes a SHA-256 of every attachment — the one piece of real provenance in the codebase. ZIP extraction guards against path traversal in member names and caps at 20 members. Extracted attachment text is scanned for instruction-like language and flagged as data-only.

**Defects.** Comment pagination (DL-P1-010) — 20 of 137 in the probe, `total` ignored, no disclosure, and `important_comments = comments[-5:]` of that arbitrary page. Attachment fan-out is unbounded (DL-P2-024). MIME comes from Jira metadata and is never validated against content (only PNG is sniffed, for dimensions). PDF extraction is a regex over `(...)` literals — it will produce garbage on compressed streams and does at least emit `"UNKNOWN: PDF text could not be extracted"` when it finds nothing. `image_analysis` is a constant string. `post_comment` sends plain text in a single ADF paragraph — correct, but it means DevLens can never post a formatted report.

**Screenshot invariant:** enforced in code, as described in §14.

---

## 16. GitHub Review

**Verified good.** SHA validation is rigorous (`len == 40` ∧ all hex) on `resolve_ref`, `get_pull_request.head` and every commit. `list_files` refuses a truncated tree outright rather than silently analysing a subset — exactly the right call. `read_file` checks the declared size *and* the decoded length against 100 000 and requires base64 encoding. Blob filtering excludes symlinks and submodules by mode (`100644`/`100755` only). Redirects are disabled. `evidence_url` is verifiably commit-pinned and percent-encodes the path:
```
https://github.com/org/repo/blob/bbbb…bbbb/src/a%20b.py#L12
```
Malformed responses raise a sanitised `ProviderError`.

**Defects.** `Link: rel="next"` is never read for PR files or commits (DL-P1-010) — reproduced: a 250-file PR yields 100 files and the header is discarded. No handling of binary files, renames (`previous_filename`), or deletions beyond the `status` string. Fork PRs are not distinguished, so `head_sha` may belong to an untrusted fork and nothing marks it. No rate-limit handling: `X-RateLimit-Remaining` and `Retry-After` are ignored; a 403 secondary-rate-limit response becomes a generic `ProviderError`. Large diffs are capped at 2 MB with no notice in the report.

---

## 17. Bitbucket Review

**Verified good, and in one respect better than the GitHub adapter.** The `next`-link handling in `list_files` is genuinely careful: it detects pagination cycles, caps at 500 pages, and validates that the `next` URL has no userinfo, no fragment, the same scheme/host/port as the base, and a path under the expected prefix before following it. That is a real SSRF/pagination-poisoning defence and it has no equivalent in the GitHub adapter. `HEAD` correctly resolves `mainbranch.name`. Files with `link`, `subrepository` or `binary` attributes are excluded. Path segments are validated against `.`/`..`/empty. Basic vs Bearer auth is selected by the presence of `git_email`, matching Atlassian's actual API.

**Defects.** The directory walk costs one request per directory (DL-P2-021) versus GitHub's single recursive call — a structural asymmetry that will surprise anyone who benchmarks the two. The careful `next` handling in `list_files` is *absent* from `get_pull_request_files` and `list_commits`, which ignore `next` entirely (DL-P1-010). `read_file` checks for NUL bytes but not size before decoding (the 100 KB cap is enforced by `max_bytes` at the transport, which is correct but implicit).

**GitHub assumptions leaking in.** Two: `WriteGateway._pr_refs` defaults the base branch to `"main"` for both providers, ignoring `mainbranch`; and `PullRequestFile.status` is populated from Bitbucket's `status` vocabulary while `review_heuristics.missing_tests` tests it against GitHub's (`{"added","renamed"}`). Bitbucket diffstat emits `added`/`removed`/`modified`/`renamed`, so this mostly works — by luck, not by design.

---

## 18. Sandbox Review

**Repository reading vs repository code execution — the distinction that matters.** Reading is safe: `read_file` is size- and encoding-checked, `Workspace.resolve` correctly resolves symlinks before the `is_relative_to` containment check, `open_local` refuses non-directories and non-git paths, `authorize_analyze_path` resolves both sides before comparing. I could not construct a traversal escape from `Workspace.resolve` — the check is done on the resolved path, which is the correct order and the one most implementations get wrong. **REJECTED: no path-traversal escape from a configured root.** (High confidence, subject to DL-P1-012: by default there *is* no configured root.)

Execution is unsafe in every dimension:

| Control | Present? |
|---|---|
| Environment scrubbing | **✗** — `os.environ.copy()` (DL-P0-001, reproduced) |
| User / uid separation | ✗ — same user as the API |
| Filesystem isolation | ✗ — full host FS visible; only `cwd` is set |
| Network isolation | ✗ — full outbound network |
| Memory / CPU / pid / disk limits | ✗ — none |
| Process-group kill on timeout | **✗** — child orphaned (DL-P1-011) |
| Package installation | ✓ blocked by the allowlist (`install_dependency` never invoked) |
| Build commands / arbitrary shell | ✓ blocked by the allowlist |
| Git hooks | ⚠ `git clone` runs no hooks from the remote; `git checkout` can run none. Low risk |
| Submodules | ✓ not fetched (`--depth 50` without `--recurse-submodules`) |
| Git LFS | ⚠ if `git-lfs` is installed in the image it runs smudge filters on checkout — **NOT VERIFIED** |
| Credential persistence in the clone | ✓ auth is passed via `GIT_CONFIG_*` env, never written to `.git/config`; `origin` is removed after clone |
| Disposal | ✓ `rmtree` in `__aexit__` |

**Verdict.** Safe for **reading** trusted internal repositories. **Not safe for executing** any repository, including internal ones, because an internal repository is exactly what a malicious insider or a compromised CI token would use. Do not overstate the mitigation Docker provides: it bounds the blast radius to the container, but every credential DevLens holds lives in that container.

---

## 19. Security & Threat Model

| # | Threat | Current mitigation | Gap | Severity | Recommendation |
|---|---|---|---|---|---|
| 1 | **Malicious repository → code execution** | `--run-tests` is opt-in; command allowlist | Full env inherited; no isolation; no limits (reproduced) | **P0** | DL-P0-001 |
| 2 | **Malicious Jira content → prompt injection** | Attachment text scanned for "ignore … instructions"; LLM has no tools; output is one summary string | Repository content is concatenated into the prompt unlabelled and undelimited; no scan on the path that actually reaches the model | **P2** | §23 |
| 3 | **Stolen git token** | Repository allowlist enforced twice; `merge_pr`/`deploy` denied unconditionally | Token has whatever scope you gave it; DevLens cannot constrain that | P2 | Document fine-grained PAT scopes; verify scope at startup |
| 4 | **Stolen Jira token** | Project-key allowlist; write-back needs approval | Same | P2 | Same |
| 5 | **Path traversal** | Resolve-then-contain, correctly ordered | **No root configured by default** | **P1** | DL-P1-012 |
| 6 | **SSRF** | Jira host pinned; Bitbucket `next` host/scheme/path pinned; redirects disabled on git/Jira | `ObserveHub`/LLM base URLs are unvalidated (`http://` accepted, credentials sent in clear) | P2 | Require HTTPS for provider URLs |
| 7 | **Sandbox escape** | n/a — nothing to escape from | There is no boundary | **P0** | DL-P0-001 |
| 8 | **Remote write abuse** | Allowlist re-check, `HUMAN_ONLY`, unknown actions denied (all verified) | Approval is free text; double-execution; no audit of writes | **P1** | DL-P1-004/005 |
| 9 | **SQL abuse** | Single-SELECT parser (blocks stacking, CTE-writes, comments) | Side-effecting functions pass; no read-only credential requirement | **P1** | DL-P1-013 |
| 10 | **Secret leakage to the browser** | Tokens are `SecretStr`; `/settings` returns only booleans and names; provider errors sanitised | **REJECTED** — no path found from credentials to any response | — | Keep the `SecretStr` discipline |
| 11 | **Secret leakage to logs/audit** | `_redact` on audit; `type(exc).__name__` only for unexpected errors | Exact-key matching misses `access_token`, `refresh_token`, `x-api-key`, URL credentials | P2 | DL-P2-019 |
| 12 | **CSRF** | `SameSite=strict`; JSON-only endpoints | No token; no origin check in the local app (the hosted app has one) | P2 | Port the hosted origin check |
| 13 | **Session theft** | `HttpOnly` | No `Secure`, no expiry, no rotation, no binding | P2 | DL-P2-020 |
| 14 | **Brute force** | none | 30+ attempts, no throttle (reproduced) | P2 | Port the hosted login quota |
| 15 | **Resource exhaustion** | `max_pending=32`, `Semaphore`, per-file 100 KB, tree 2 000, diff 2 MB, evidence 20 | Unbounded: subprocess output (measured 19 MB), attachment count, audit file, in-memory stores, SSE connections | **P1** | DL-P1-011, DL-P2-024 |
| 16 | **Evidence forgery / self-deception** | — | Provider failures become supporting evidence (reproduced) | **P0** | DL-P0-002 |
| 17 | **Supply chain (frontend)** | `package-lock.json` committed | Monaco executed from a public CDN at runtime | **P1** | DL-P1-017 |

**Prompt injection, examined specifically (§23).** The LLM is reachable on exactly one path: `AskWorkflow` → `guard.complete(prompt)`. The prompt is built by flat concatenation with no trust delimiters:
```
checkout
Workspace /tmp/tmpket93zpe.
Search 'checkout': payment.py:1:# checkout handler
payment.py:4:def checkout(): pass
Logs UNAVAILABLE: provider is not configured. …
```
Repository content — the most attacker-controllable input in the system — is inlined verbatim with no "the following is untrusted data" framing, no delimiter and no injection scan (the scan that *does* exist runs on Jira attachment text, which never reaches a prompt). **The blast radius is genuinely small**, and this should be stated plainly rather than inflated: the model has no tools, no ability to approve anything, and its sole output is the `executive_summary` string. The realistic outcome of a successful injection is a poisoned summary presented as DevLens's answer — a report-integrity problem, not RCE and not exfiltration. Fix by fencing retrieved content in a delimited block with an explicit data-only instruction, and by applying the same injection scan to repository content that already exists for attachments.

**Trust boundary diagram (§77).**
```
╔═ Browser ═══════════════╗   cookie (HttpOnly, SameSite=strict, ✗Secure)
║ operator session        ║────────────────────────────────┐
║ ⚠ + cdn.jsdelivr.net    ║                                │
╚═════════════════════════╝                                ▼
                                    ╔═ DevLens process ═══════════════════╗
                                    ║ ALL SECRETS LIVE HERE:              ║
                                    ║  DEVLENS_JIRA_TOKEN                 ║
                                    ║  DEVLENS_GITHUB/BITBUCKET_TOKEN     ║
                                    ║  DEVLENS_*_API_KEY / OAuth tokens   ║
                                    ║  DEVLENS_LOKI/PROM/TEMPO/SQL_TOKEN  ║
                                    ║  DEVLENS_UI_PASSWORD                ║
                                    ╚══╤════════╤══════════╤══════════╤═══╝
     Authorization: Basic/Bearer ──────┘        │          │          │
     ▼                                          ▼          ▼          ▼
  ┌ External SaaS ─────────┐   ┌ Repository sandbox ─┐  ┌ Telemetry ┐ ┌ LLM ┐
  │ Jira · GitHub ·        │   │ tempdir + git clone │  │ Loki/Prom │ │ ext │
  │ Bitbucket              │   │ ███ NO BOUNDARY ███ │  │ Tempo/SQL │ │ API │
  │ token crosses out      │   │ INHERITS ALL SECRETS│  │ token out │ │ key │
  └────────────────────────┘   │ ▲ repository code   │  └───────────┘ └─────┘
                               └─┴─── DL-P0-001 ─────┘
```
Every secret crosses exactly one boundary it should not: into the sandbox.

**Multi-project isolation (§62).** Verified by probe: project A's `ReadPolicy` denies project B's repository (`"Repository is not allowed."`) and B's Jira project key (`"Jira project is not allowed."`). `ProjectAgents._run` looks up the agent by `request.project` and raises `AccessDenied` for unknown projects; each project gets its own HTTP clients and credentials. **One asymmetry:** repository matching is case-**insensitive** (`ORGA/REPO` is allowed) while Jira project matching is case-**sensitive**. Deliberate or not, it should be documented. `ProjectAgents.analyze` (local path) ignores the project entirely and uses whichever agent iterates first — harmless today because local analyze touches no project credentials, but it is an isolation hole waiting for a future change.

---

## 20. Observability Review

**As a consumer of telemetry.** Four providers, each a thin HTTP client. Loki: `query_range` with `limit=20`, **no time range** — so the query window is whatever the server defaults to, and the report cannot state what period it examined. Prometheus: instant `api/v1/query` only, no `query_range`, so no trend evidence is possible. Tempo: free-text `q`, 10 traces, returns trace IDs only. SQL: see DL-P1-013. No provider has a resilience policy, a retry, or a distinct timeout. The clients are created by `ObserveHub.from_env()` and **never closed** — each construction leaks up to four `AsyncClient` instances.

**The availability semantics are the headline problem** (DL-P0-002). DevLens must distinguish four states and currently distinguishes one:

| State | Should be | Is |
|---|---|---|
| Not configured | UNKNOWN + "not configured" | ✓ correct |
| Configured, unreachable | UNKNOWN + "unavailable" | ✗ **SUPPORTED + error text** |
| Configured, reachable, no data | UNKNOWN + "no matching data" | ✗ **SUPPORTED + sentinel string** |
| Configured, reachable, data | SUPPORTED | ✓ correct |

**Provider error isolation (§61)** is implemented correctly and deserves credit: `ObserveHub.collect` catches per provider, so Prometheus being down does not fail the run — the investigation completes as a partial result, which is exactly the desired behaviour. The defect is purely in how that partial result is *labelled*.

**As a producer of telemetry — DevLens has none.** No structured logging (`logging` is imported only in `saas.py`), no metrics endpoint, no tracing, no request ids in the local app, no health detail beyond `{"status":"ok"}`. A tool whose `observe` workflow reports *"Missing: structured logs, metrics, traces, correlation IDs"* about other people's code emits none of those itself. The hosted gateway does emit a structured JSON access log with a request id, correlation fields and explicit exclusion of bodies and credentials — that is the right shape and should be backported.

---

## 21. Reliability / Job Execution Review

**The current model.** One `asyncio.Task` per job inside the API event loop; a dict of live tasks; SQLite for state; `max_pending=32`; a hardcoded 120 s per job; no queue; no worker; no leases; no heartbeats.

**Answers to §7 of the brief.**
- **Maximum reasonable concurrency:** about **4–8 concurrent jobs** on one worker. The binding constraints, in order: the event loop is shared with request serving and is blocked by synchronous `rglob`/`glob` and by `communicate()` reads (DL-P1-011); SQLite is in rollback-journal mode with a 2 s busy timeout and an unindexed `events` scan at 20 Hz per open SSE stream (DL-P2-030); every job holds provider HTTP connections from a pool sized `concurrency` (default 5).
- **Process restart:** jobs are permanently stranded in `running` (DL-P1-008, reproduced). Approvals, knowledge, onboarding, sessions and write idempotency are all lost.
- **Multiple workers:** **broken.** `uvicorn --workers N` gives each worker its own `JobService.tasks`, its own `AuthGate.sessions`, its own `ApprovalStore` and its own `WriteGateway._seen`, all against one SQLite file. A job submitted to worker 1 is invisible to worker 2's cancel; a session issued by worker 1 is rejected by worker 2; the write idempotency cache is per-worker. The hosted app takes an `flock` to enforce one worker; the local app has no such guard and nothing warns you.
- **Locking / contention:** `BEGIN IMMEDIATE` per write via the `with conn` context; readers block writers under rollback journal; 2 s timeout then `OperationalError: database is locked`, which `_execute` catches as a generic `Exception` and records as `"OperationalError"`.
- **Long-running jobs:** the 120 s budget is not configurable in the local app (DL-P2-029). A clone + `pytest` of a real repository will frequently exceed it, and the result is `failed` with `"Job exceeded its time budget."` after the work has been done.
- **Memory growth:** `AuditLog.records`, `ApprovalStore.records`, `KnowledgeStore.documents` (capped at 1 000 only in the persistent subclass), `AuthGate.sessions` and `WriteGateway._seen` all grow without bound in the local app.
- **Queue starvation:** none — there is no queue. `submit` raises `ProviderError("Job capacity reached")` at 32, which the API translates to **502** (should be 429 or 503 with `Retry-After`).
- **Cancellation:** reliable for awaits, unreliable for subprocesses — `task.cancel()` does not kill `pytest`, and `run()` never kills the child (DL-P1-011). A cancelled review leaves a running test suite.
- **Duplicate execution / idempotency:** `POST /jobs` has no idempotency key, so a double-click creates two runs. There are no retries anywhere, so no retry storms and no retry-induced duplicate remote writes — the duplicate-write path is the approval race (DL-P1-004), not retries.

**When does this architecture stop being sufficient?** Concretely: **(a)** the moment you need more than one worker process, **(b)** the moment a single job legitimately exceeds ~2 minutes, or **(c)** at roughly 10 concurrent runs. Until then it is the correct design — in-process tasks plus SQLite is far cheaper to operate than any queue, and the audit found no defect that a queue would fix. The right next step is **not Redis/Celery/Temporal** but a `JobExecutor` protocol with the current `InProcessExecutor` as the only implementation, so the seam exists before you need it. Concurrency past a few dozen would then justify a second implementation; not before.

---

## 22. Performance Review

All numbers measured in the audit environment (Linux, Python 3.11.15, Node 22). Reproduce before acting on any of them.

| Operation | Measured | Note |
|---|---|---|
| `import devlens.app.api` | **0.33 s** | fine |
| Lifespan startup (unconfigured) | **0.001 s** | fine |
| `GET /health` | **0.32 ms** median | fine |
| `GET /dashboard` | **0.87 ms** median | fine |
| `GET /jobs` | **0.70 ms** median | fine |
| `POST /analyses/observe` (2 000-file tree) | **0.27 s** | fine |
| `POST /analyses/analyze` (same tree, with `query`) | **60.64 s** | ← **measured bottleneck** |
| ↳ `repo.tree()` | 0.24 s | |
| ↳ `repo.status()` / `log()` / `show()` / `diff()` | ≤ 0.15 s each | |
| ↳ **`repo.search()`** | **60.07 s** | hits the 60 s command timeout |
| ↳ same ripgrep invoked directly | **0.056 s**, 19 427 774 bytes | |
| `ticket` workflow | 22 GitHub + 1 Jira calls; 20 concurrent file reads | shape is right |
| pytest (175 tests, coverage on) | **10.1 s** | good |
| `npm ci` | **6.5 s** | |
| `vite build` | **2.8 s** | |
| Bundle | **482.69 kB JS** (150.07 kB gzip) + **25.57 kB CSS** | Monaco excluded — it is fetched from a CDN |
| Docker build | **NOT VERIFIED** — no daemon available | |

**Measured bottleneck (one):** `run()` reading large subprocess output through asyncio pipes — 1 000× slower than the subprocess itself (DL-P1-011). Everything else measured is fast.

**Potential future bottlenecks (not measured, do not optimise yet):** SQLite contention under concurrent SSE (DL-P2-030); Bitbucket per-directory tree walk (DL-P2-021); `ast.get_source_segment` quadratic behaviour on large review files (DL-P3-042); the 482 kB bundle on slow networks. None of these justifies work today.

---

## 23. Testing & CI Review

**Backend tests.** 175 tests, 5 713 lines, **10.1 s**, 99.96% statement+branch coverage. Genuinely good in several respects: `httpx.MockTransport` fixtures test real provider parsing against realistic payloads rather than mocking the adapters away; `conftest.py` resets `app.state` per test, which prevents credential bleed between tests; error paths and malformed-response handling are covered; and there are real security-boundary tests — `test_shell_allowlist_blocks_writes`, `test_local_git_provider_and_path_escape`, `test_write_operations_are_denied`, `test_access_denied_before_network`, `test_capability_guard_denies_deferred_features`, `test_every_write_route_requires_authentication`, `test_cookie_csrf_logout_and_invalid_bearer`.

**Do the tests assert behaviour or implementation?** Mostly behaviour. But three modules — `test_coverage_fill.py` (732), `test_last_gaps.py` (331), `test_unit_gaps.py` (382) — are 1 445 lines, a quarter of the suite, named after a coverage metric. That is the signature of tests written to reach a number rather than to describe a contract, and it shows: they are the modules whose failures would be hardest to interpret.

**Testing the tests (§53).** 99.96% coverage did **not** catch a single one of the seventeen P0/P1 findings in this report. Every one of them is a behaviour the tests execute without asserting the thing that matters:

| Uncovered behaviour | Finding |
|---|---|
| Provider failure → evidence status | DL-P0-002 |
| Child-process environment contents | DL-P0-001 |
| UI mount under a non-editable install | DL-P1-003 |
| Concurrent approval of one record | DL-P1-004 |
| Job state across a restart (local app) | DL-P1-008 |
| SSE event ordering and liveness | DL-P1-009 |
| Pagination beyond page 1 | DL-P1-010 |
| Large subprocess output / timeout kill | DL-P1-011 |
| Generic vendor env vars enabling the LLM | DL-P1-007 |

This is the single most useful thing the coverage number tells you: **it is measuring line execution, not behaviour, and the project is treating it as a quality gate.** Note also that the security tests for cookies and CSRF live in `test_saas.py` — they test the hosted app, not the one the README tells you to deploy.

**Frontend tests.** **None.** No test runner, no test script, no component tests, no mocks, no routing tests, no SSE tests. `tsc --noEmit` passes but is not wired into `npm run build` (DL-P3-036). For ~2 200 lines of UI at this maturity, a handful of smoke tests on the four run workspaces would be proportionate; a full component suite would not.

**End-to-end tests.** None, and none of the eight flows the brief lists (§52) is automatically tested end to end. The existing `httpx.MockTransport` fixtures are already the right substrate — the gap is an orchestration layer, not a fixture strategy. Remote providers should stay on controlled fixtures in CI; no live credentials are needed for any of the eight.

**CI.** Absent (DL-P1-016). For an MVP the proportionate gate is one workflow: pytest + coverage-at-actual-threshold, ruff, `tsc --noEmit`, `vite build`, `docker build`, and a container smoke test hitting `/health` and `/`. That last check alone would have caught DL-P1-003. Dependency scanning (`pip-audit`, `npm audit`) is worth adding; SAST is not, yet.

**Dependency review (§54).** Python: four runtime dependencies (`fastapi`, `httpx`, `pydantic`, `uvicorn`), all current, all used, no duplicates, no unmaintained packages, upper bounds pinned below the next major. This is an exemplary dependency footprint. Frontend: `react`, `react-dom`, `@xyflow/react` (used only for the fake architecture canvas — remove it with DL-P2-026 and save ~150 kB), `@monaco-editor/react` and `monaco-editor` (the latter declared but never bundled, DL-P3-037). No dependency upgrade is recommended on version-currency grounds alone.

**Configuration review (§55).** Every `DEVLENS_*` variable read by the code:

| Variable | Used | Documented | Validated | Secret | Default safe |
|---|---|---|---|---|---|
| `DEVLENS_JIRA_URL` | ✓ | ✓ | ✓ strict | — | ✓ |
| `DEVLENS_JIRA_EMAIL` / `_TOKEN` | ✓ | ✓ | ✓ non-empty | ✓ | ✓ |
| `DEVLENS_GIT_PROVIDER` | ✓ | ✓ | ✓ literal | — | ✓ |
| `DEVLENS_GITHUB_TOKEN` / `DEVLENS_BITBUCKET_TOKEN` / `_EMAIL` | ✓ | ✓ | ✓ non-empty | ✓ | ✓ |
| `DEVLENS_REPOSITORIES` / `DEVLENS_JIRA_PROJECTS` | ✓ | ✓ | ✓ pattern | — | ✓ |
| `DEVLENS_CONFIG` | ✓ | ✓ | ✓ TOML + model | — | ✓ |
| `DEVLENS_UI_PASSWORD` | ✓ | ✓ | ✗ no strength check | ✓ | **✗ unset = open** |
| `DEVLENS_JOBS_DB` | ✓ | ✓ | ✗ | — | ✓ |
| `DEVLENS_ANALYZE_ROOT` | ✓ | ✓ | ✓ resolved | — | **✗ unset = unrestricted** |
| `DEVLENS_AUDIT_LOG` | ✓ | ✓ | ✗ | — | ✓ |
| `DEVLENS_ALLOW_WRITES` | ✓ | ✓ | ✓ enum | — | ✓ |
| `DEVLENS_LLM_PROVIDER` / `_API_KEY` / `_BASE_URL` / `_MODEL` | ✓ | ✓ | ✗ no URL scheme check | ✓ | ✓ |
| `DEVLENS_VISION_MODEL` | ✓ | ✓ | ✗ | — | ✓ |
| `DEVLENS_ANTHROPIC_API_KEY` / `_BASE_URL` / `DEVLENS_CLAUDE_MODEL` | ✓ | ✓ | ✗ | ✓ | ✓ |
| `DEVLENS_CODEX_*` (7 variables) | ✓ | ✓ | ✗ | ✓ | ✓ |
| `DEVLENS_CLAUDE_OAUTH_TOKEN` / `_CREDENTIALS` / `_CLIENT_ID` | ✓ | ✓ | ✗ | ✓ | ✓ |
| `DEVLENS_LLM_OAUTH` | ✓ | ✓ | ✓ enum | — | ✓ |
| `DEVLENS_LOKI/PROM/TEMPO/SQL_URL` + `_TOKEN` | ✓ | ✓ | **✗ `http://` accepted** | ✓ | ✓ |
| `DEVLENS_SAAS_CONFIG` | ✓ | **✗ undocumented** | ✓ strict | — | ✓ |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `CODEX_ACCESS_TOKEN` / `ANTHROPIC_PROFILE` / `ANTHROPIC_CONFIG_DIR` / `CODEX_HOME` | ✓ | partially | ✗ | ✓ | **✗ silently enable the LLM (DL-P1-007)** |

No dead variables. Two unsafe defaults (`DEVLENS_UI_PASSWORD`, `DEVLENS_ANALYZE_ROOT` — the latter fixed by Compose but not by `docker run`). One undocumented variable (`DEVLENS_SAAS_CONFIG`). Six generic vendor variables consulted without documentation.

---

## 24. Docker / Deployment Review

**NOT VERIFIED by execution** — no Docker daemon was available. The following is a source review plus one reproduced defect.

**Good.** Two-stage build (node → python slim). Non-root `USER devlens` (uid 10001) with `/data` and `/workspace` pre-chowned. `git` and `ripgrep` installed with `--no-install-recommends` and apt lists cleaned. A `HEALTHCHECK` that actually hits `/health`. No secrets baked in; `.dockerignore` excludes `.env`, `devlens.toml`, `.git`, `tests`, `.venv`, `node_modules` and the SQLite file. Compose binds `127.0.0.1:8000` (correct and matching the README), mounts `./workspace` **read-only**, sets `DEVLENS_ANALYZE_ROOT=/workspace` (which fixes DL-P1-012 for the Docker path), and persists jobs in a named volume.

**Defects.**
1. **The UI is not served** (DL-P1-003) — reproduced. This invalidates the README's primary Docker instruction.
2. No `--pids-limit`, `mem_limit`, `cpus`, `read_only: true`, `cap_drop: [ALL]` or `security_opt: [no-new-privileges:true]` in Compose. Given that the container executes repository code (DL-P0-001), these are the cheapest available mitigations and none is present.
3. `pip install .` copies `src` before installing, so any source change busts the layer — acceptable for a small image, but there is no dependency-only install step to cache.
4. No base-image digest pinning (`python:3.12-slim`, `node:22-bookworm-slim` are floating tags); no apt version pinning.
5. `restart: unless-stopped` combined with DL-P1-008 means every restart accumulates permanently-`running` job rows.
6. The `saas.py` entry point — the one with the security controls — has no Dockerfile target, no Compose service and no documentation.
7. `DEVLENS_ANALYZE_ROOT=/workspace` is set in Compose but **not** in the Dockerfile, so a naive `docker run devlens` has no path restriction (the README's own `docker run` example does set it).

---

## 25. UX / Design System / Accessibility Review

**Does it feel like an engineering tool or an LLM wrapper?** **An engineering tool** — and this is the design's real achievement. There is no chat window anywhere. The information architecture is Context / Activity / Evidence / Hypotheses / Findings, which is how engineers actually reason. The copy is unusually disciplined: *"This is not a chat thread."*, *"What was inspected, not a hidden chain of thought."*, *"Sections without evidence stay UNKNOWN. Numbers are not invented."*, *"Calculated locally. Not invented by a model."*, *"Screenshots stay UNKNOWN. They are never treated as root cause."* Evidence cards carry type icons and a status chip. The Ask flow is explicitly Question → Sources → Reasoning → Answer. Someone thought hard about epistemics here, and it shows.

**Can a user tell what is fact, hypothesis, unknown, failed and pending?**

| Question | Answer |
|---|---|
| What is DevLens doing? | **No** — the timeline shows two events (DL-P1-009) |
| What evidence did it find? | **Yes** — the evidence pane is good |
| What is fact vs hypothesis? | **Partly** — the chips are there, but they are wrong under provider failure (DL-P0-002) and share a palette with severity (DL-P1-015) |
| What failed? | **Partly** — `job.error` renders with an impact line, but a failed *provider* renders as a success |
| What remains unknown? | **Yes** — Unknowns is a first-class section on every surface |
| What needs approval? | **Yes** — dedicated page + notifications |

**Fake activity indicators and capability implications — the honest list.** The "Timeline" panel with *"Waiting for the first engineering action…"* (DL-P1-009). The Integrations connect wizard (DL-P2-026). The Repositories detail tabs (DL-P2-026). The Design "Architecture" canvas showing an invented pipeline for every design (DL-P2-026). Hardcoded capability copy that contradicts the running configuration (DL-P2-027). `tokens — · cost —` placeholders. The Services detail page's nine tabs. Each of these implies a capability DevLens does not have — which is precisely the failure mode a tool that sells epistemic honesty cannot afford.

**Design system.** Coherent tokens, a real light theme, consistent components. Two defects: severity and evidence status share token values (DL-P1-015) and the light theme does not re-derive the semantic ramp (DL-P2-034). `.sev-P0`/`.sev-Critical` are styled but never produced; `.sev-P4` is produced but never styled.

**Accessibility.**

| Check | Result |
|---|---|
| Keyboard navigation | Partial — links/buttons are native; modals have no trap or restore |
| Focus states | ✓ global `:focus-visible` with a 2 px indigo outline |
| Labels | Partial — inputs use `<label class="field">` wrapping; icon buttons have `aria-label`; the theme toggle's label never changes with state |
| ARIA | ✗ no `role="dialog"`, no `aria-modal`, no `aria-live` on async regions, no `aria-current` on the active nav item |
| Contrast | ✗ severity/status chips fail AA in light mode (DL-P2-034) |
| Screen readers | ✗ run completion is never announced |
| Form errors | Partial — errors render as `<p class="error">` not associated via `aria-describedby` |
| Modal behaviour | ✗ no trap, no restore; `Escape` works for two of four modals |
| Reduced motion | ✓ `@media (prefers-reduced-motion: reduce)` present |
| Colour dependence | **✓ correct** — every chip renders `● CONFIRMED`, never a bare dot. This is the requirement the brief emphasises and DevLens meets it |

---

## 26. README Accuracy Review

The README is, overall, **more accurate than the norm** — it explicitly disclaims the service catalog, code generation, unapproved merge/deploy, screenshot-only root cause, confidence percentages and multi-tenancy, and each of those disclaimers is true. The following claims are not.

| # | Claim | Actual behaviour | Evidence | Correction |
|---|---|---|---|---|
| 1 | "The image includes the FastAPI backend, **production web build**… Open http://127.0.0.1:8000/" | No static mount is created; `GET /` returns 404 | `_UI` resolves to `<venv>/lib/python3.11/web/dist`; `mounts: []` | Fix DL-P1-003, then the claim becomes true |
| 2 | "Leave unset for fully deterministic behavior" (LLM) | `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `CODEX_ACCESS_TOKEN` in the environment enable `llm` + `screenshot_root_cause` | Reproduced, probe 2A | Fix DL-P1-007, or state which non-DevLens variables are consulted |
| 3 | "`python -m pytest -q --cov=devlens --cov-fail-under=100`" | Exits non-zero: 99.96% | Executed: `FAIL Required test coverage of 100% not reached` | Close the two branches or publish the real threshold |
| 4 | "**SELECT-only** queries via HTTP gateway" | Statement shape only; `pg_read_file`, `lo_import`, `pg_sleep`, `xp_cmdshell` all pass | Reproduced, probe 3 | "Single-SELECT statements only. Use a read-only database role — DevLens does not restrict function side effects." |
| 5 | "Optional `DEVLENS_AUDIT_LOG`: JSONL audit trail **without secrets**" | Only exact key names are redacted | Reproduced, probe 10 | Fix DL-P2-019, or say "best-effort redaction of common credential keys" |
| 6 | "`GET /jobs/{id}/events` **SSE progress stream**" | Two events total; workflow timeline replayed after completion | Reproduced, probe 6 | "Run log stream" until DL-P1-009 is fixed |
| 7 | Git write actions "`create_branch`, `push_commit`, `create_pr`, `post_pr_comment`" | `push_commit` creates a branch at HEAD and pushes nothing; all bodies are fixed boilerplate | Reproduced, probe 7 | Rename the action; state that comment/PR bodies do not yet carry the analysis |
| 8 | "**Resilience:** Per-client rate limit, circuit breaker" | One policy is shared between the Jira and git clients; timeouts never trip the breaker | `dependencies.py:93-123`; `CircuitBreaker.call` catches `Exception`, not `CancelledError` | "Per-project" — or fix DL-P1-014 and keep the claim |
| 9 | `service_catalog` "**Not implemented** (always denied)" | `/services` returns a derived catalog and the UI ships a Services section | `catalog.services()`, `web/src/pages/Catalog.tsx` | Distinguish *auto-discovery* (absent) from *derived catalog* (present) |
| 10 | "Multi-tenant org RBAC, SSO, or team management" is out of scope | `saas.py` + `hosting.py` + `identity.py` implement tenants, roles, identities and sessions (600 LOC, tested) | — | Document the hosted entry point or remove it |
| 11 | "Primary surfaces: … Integrations, Settings, Runs, Approvals, Knowledge, Services, Designs" | Integrations' connect flow, Repositories detail and the Design architecture canvas are non-functional | §11 | Mark the stubs, as the README already does for Settings |
| 12 | Review: "files + Monaco diff + findings" | Monaco is fetched from `cdn.jsdelivr.net` at runtime; the view does not work offline | Found in `dist/assets/*.js` | Disclose, or self-host (DL-P1-017) |
| 13 | "Job history persists in a Compose volume" | True — but jobs interrupted by a restart stay `running` forever | Reproduced, probe 10 | Fix DL-P1-008 |

---

## 27. Architecture Debt

Every architectural issue, classified by the gate it must clear (§67).

| Issue | Classification |
|---|---|
| Docker UI path (DL-P1-003) | **Fix now** |
| Interrupted-job recovery in the local app (DL-P1-008) | **Fix now** |
| LLM auto-enable from generic env vars (DL-P1-007) | **Fix now** |
| No CI (DL-P1-016) | **Fix now** |
| Severity / evidence-status / confidence-band conflation (DL-P1-015) | **Fix now** — cost grows with every consumer |
| `ProviderStatus` and the evidence-status invariant (DL-P0-002) | **Fix now** — it is the product thesis |
| Dead security tables and dead providers (DL-P2-018, DL-P2-032) | **Fix now** — cheap, and they mislead every reader |
| Approval → Proposal model (DL-P1-005) | **Fix before write-back** |
| Approval concurrency + idempotency keyed on the approval (DL-P1-004) | **Fix before write-back** |
| Write payloads carrying the actual analysis (DL-P1-006) | **Fix before write-back** |
| Approval persistence (currently in-memory) | **Fix before write-back** |
| Audit of request → decision → execution | **Fix before write-back** |
| Sandbox environment scrubbing + rlimits (DL-P0-001) | **Fix now** if `--run-tests` is used at all; **fix before untrusted repositories** for real isolation |
| Process-group kill and streamed output (DL-P1-011) | **Fix now** |
| SQL read-only credential requirement (DL-P1-013) | **Fix before production telemetry** |
| Circuit-breaker scoping and timeout accounting (DL-P1-014) | **Fix before production telemetry** |
| Time ranges and `query_range` for Loki/Prometheus | **Fix before production telemetry** |
| Pagination beyond page 1 (DL-P1-010) | **Fix before production telemetry** |
| `ToolCall` / `Run` as first-class persisted objects | **Fix before production telemetry** — reproducibility depends on it |
| `JobExecutor` seam | **Fix before multiple users** |
| Session store, approval store, knowledge store out of process memory | **Fix before multiple users** |
| Single-worker assumption (no lock in the local app) | **Fix before multiple users** |
| SQLite WAL + indexes (DL-P2-030) | **Fix before multiple users** |
| Real auth (identities, expiry, rate limit) in the local app (DL-P2-020) | **Fix before multiple users** |
| Postgres, a queue, per-tenant isolation | **Fix before SaaS** |
| Per-tenant credential vault (env vars are process-global today) | **Fix before SaaS** |
| Global job ids without a tenant namespace | **Fix before SaaS** |
| Bitbucket tree-walk strategy (DL-P2-021) | **Can wait** |
| Lexical tokenisation improvements (DL-P2-022) | **Can wait** |
| Frontend component decomposition | **Can wait** |
| Accessibility polish (DL-P2-035) | **Can wait** |

**Future multi-tenancy readiness (§63).** Four assumptions would be expensive later, and all four are *cheap to note now and dangerous to forget*: credentials are read from process-global environment variables (`ObserveHub.from_env`, `LlmProvider.from_env`) rather than injected per tenant; job ids are unnamespaced UUIDs in one table; capability state is process-global; and `DEVLENS_ANALYZE_ROOT` is a single process-wide path. `saas.py` works around the first three by constructing an isolated child application per tenant — which is a legitimate pattern at small scale and does not scale past a few dozen tenants. **None of this is a current bug.** Classify as future architecture debt and do not act on it yet.

---

## 28. What Is Already Good

Stated plainly, because the finding count above is not a fair summary of the work.

1. **The `ticket` workflow is excellent.** Bounded at every step, honest about every cap, genuinely commit-pinned, and it tells you "top 20 of 41 eligible files" without being asked. This is what the whole product should feel like.
2. **Deny-by-default is real, not decorative.** `CapabilityGuard` computes from the environment once, never from a provider response, and `_need()` raises before any client is touched. Verified by execution.
3. **Authorization is at the right boundary.** `ContextTools` checks `ReadPolicy` before every provider call, and `WriteGateway` re-checks independently before every mutation. This is exactly the structure §83 of the brief argues for, and it is already here.
4. **Provider abstraction is clean.** Protocol + strategy table + factory. Adding GitLab is a table entry and an adapter; nothing else moves.
5. **Input validation is tight.** Pydantic patterns on ticket keys, repositories and project names; a genuinely strict Jira URL validator; 40-hex SHA validation on every commit from every provider.
6. **Untrusted-response handling is careful.** Bitbucket's `next`-link validation (host, scheme, port, path prefix, cycle detection, page cap) is better than most production code. GitHub's refusal to analyse a truncated tree is the right call.
7. **Secrets do not reach the browser.** `SecretStr` throughout, `/settings` exposes only booleans and names, provider errors are sanitised at the boundary. No leak path was found.
8. **The screenshot invariant is enforced structurally**, not by prompt wording — the strongest form available.
9. **No fabricated confidence anywhere.** A full search for percentages and scores found none. The capacity calculator explicitly says it is computed locally.
10. **The UI's epistemic vocabulary is genuinely well designed.** Evidence, Hypotheses, Unknowns, Limitations as first-class surfaces; no chat window; copy that repeatedly refuses to overclaim.
11. **Provider error isolation is correct** — one failed telemetry provider produces a partial investigation, not a failed one.
12. **Tests use real transport fixtures** rather than mocking the adapters away, and isolate application state per test.
13. **`saas.py` is a solid piece of security engineering** — HTTPS enforcement, `__Host-` cookies, TTLs, quotas, origin checks, security headers, single-worker locking, per-tenant isolation, and an access log that deliberately omits bodies and credentials.
14. **The dependency footprint is exemplary** — four Python runtime dependencies, all used, all current, all bounded.
15. **The README disclaims more than it oversells.** Out-of-scope lists like this one are rare and valuable.

---

## 29. What NOT to Change

Explicitly, to prevent unnecessary rewrites.

1. **Keep SQLite.** It is correct for a single-node operator tool. No defect in this report is caused by choosing SQLite; two are caused by *configuring* it poorly (WAL, indexes). Migrate only when you have (a) more than one worker process, (b) multi-tenant isolation requirements, or (c) measured contention at your real concurrency — not before.
2. **Keep the modular monolith.** Nothing here wants to be a service. Splitting it would multiply the operational surface and fix nothing.
3. **Keep in-process `asyncio.Task` execution.** Add the `JobExecutor` seam, keep the implementation. Do not adopt Celery, RQ, BullMQ or Temporal for a tool that runs a handful of concurrent investigations.
4. **Keep deterministic-by-default.** It is the product's differentiator and it works. Fix the leak (DL-P1-007); do not weaken the principle.
5. **Keep read-only-first integrations.** Read access is the default, writes are a separate capability plus an approval. The structure is right even though the approval implementation is not.
6. **Keep merge and deploy human-only, unconditionally.** `HUMAN_ONLY` is checked before the `allowed` flag, so no configuration can enable them. Verified. Do not add a flag.
7. **Keep the HTTP SQL gateway.** Not embedding a database driver is the correct boundary. Strengthen the credential requirement, keep the architecture.
8. **Keep the capability facade.** `CapabilityGuard` as the single place where optional backends are gated is the right pattern; consolidate the *other* three tables into it rather than replacing it.
9. **Keep the no-chat UI.** Resist every pull toward a conversational surface. It is the most defensible product decision in the repository.
10. **Keep `Evidence` / `Hypothesis` / `Finding` / `Unknowns` / `Limitations` as first-class output.** The shapes are right; the population logic needs fixing.
11. **Keep the hand-rolled frontend.** No router, no state library, no component library is the correct call at 2 200 lines.
12. **Keep the strict Jira URL and SHA validators.** They look paranoid; they are not.
13. **Keep the minimal dependency set.** Do not add a library to solve a problem a function solves.
14. **Do not build automatic service discovery.** See §32.

---

## 30. Quick Wins

Independently deliverable, no architectural prerequisites. Deliberately separated from the projects in §31.

**Under 1 hour**
1. Call `jobs.store.recover_interrupted()` in `api.lifespan` startup. *(DL-P1-008 — one line)*
2. `DEVLENS_UI_DIR` env override in `mount_ui`, set in the Dockerfile. *(DL-P1-003)*
3. `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, and `CREATE INDEX IF NOT EXISTS ix_events_job ON events(job_id, ts)` in `JobStore.__init__`. *(DL-P2-030)*
4. `secure=True` on the session cookie when the request is HTTPS. *(DL-P2-020)*
5. Add `REJECTED` to the `Confidence` literal and `.sev-P4` to the stylesheet. *(DL-P1-015, partial)*
6. Substring-based audit redaction: `token|secret|password|key|auth|cookie|credential`. *(DL-P2-019)*
7. Delete `policies.require`, `AUTO_PERMISSIONS`, `DENIED_PERMISSIONS`, `LocalGitProvider`, and the unused `ContextTools` facade methods. *(DL-P2-018, DL-P2-032)*
8. Fix the two stale `BOUNDARIES` messages and the `service_catalog` inventory entry. *(DL-P2-033)*
9. `"build": "tsc --noEmit && vite build"`. *(DL-P3-036)*
10. Change the job-capacity response from 502 to 429 with `Retry-After`, and non-cancellable-job from 502 to 409. *(§12)*
11. Restrict LLM auto-selection to `DEVLENS_*` variables. *(DL-P1-007)*
12. Pass an explicit minimal `env` to `run()` instead of `os.environ.copy()`. *(DL-P0-001 — the highest value-per-minute change in this report)*

**Under 1 day**
13. Fix the `_runtime` evidence-status mapping and the `InvestigateWorkflow` summary/unknowns selection. *(DL-P0-002 — the full `ProviderResult` type is a Phase-B project, but the status mapping alone removes the falsehood today)*
14. Per-record `asyncio.Lock` + reservation-before-call in the approval path. *(DL-P1-004)*
15. Stream subprocess output with an incremental cap; kill the process group on timeout or overflow; add `--glob` exclusions and `--max-filesize` to ripgrep. *(DL-P1-011)*
16. Default `DEVLENS_ANALYZE_ROOT` to the process CWD; refuse `/analyses/analyze` when no root is set. *(DL-P1-012)*
17. Self-host Monaco from `dist/assets/vs`; drop `@xyflow/react` with the fake canvas. *(DL-P1-017, DL-P2-026)*
18. Render capability copy from `/capabilities` instead of hardcoded strings; delete the Integrations wizard and the Repositories detail tabs. *(DL-P2-026, DL-P2-027)*
19. A CI workflow: pytest + ruff + `tsc --noEmit` + `vite build` + `docker build` + a `GET /` smoke test. *(DL-P1-016, and it would have caught DL-P1-003)*
20. `resource.setrlimit` via `preexec_fn` on sandbox test execution. *(DL-P0-001)*

**Under 3 days**
21. Follow `Link`/`next` pagination for Jira comments, GitHub PR files/commits and Bitbucket PR files/commits, with caps recorded in `limitations`. *(DL-P1-010)*
22. Split severity / evidence status / confidence band into three types with three colour ramps; delete `hypothesisBand`; re-derive the light theme. *(DL-P1-015, DL-P2-034)*
23. Backport the hosted security controls to `api.py`: login quota, session TTL, origin check on writes, security headers, body-size limit. *(DL-P2-020, DL-P2-028)*
24. Emit progress events from inside the workflows so the timeline becomes real. *(DL-P1-009)*
25. Per-upstream resilience policies; catch `CancelledError` in the breaker; attach policies to observability and LLM clients. *(DL-P1-014)*

---

## 31. Prioritized Remediation Plan

### Phase A — Must fix before any further feature work
True blockers only.

| Item | Finding | Why it blocks |
|---|---|---|
| Minimal environment + rlimits + process-group kill for sandbox execution | DL-P0-001 | Credential compromise, reproduced |
| Correct evidence status under provider failure | DL-P0-002 | The product's central claim is false today |
| Serve the UI from the Docker image | DL-P1-003 | The documented deployment does not work |
| Restrict LLM auto-selection to `DEVLENS_*` | DL-P1-007 | "Deterministic by default" is false |
| Recover interrupted jobs in the local app | DL-P1-008 | Permanent state corruption on every restart |
| CI with a container smoke test | DL-P1-016 | Everything above regresses silently without it |

### Phase B — DevLens core model
The domain objects that are genuinely missing, in dependency order. Each carries the migration detail §85 asks for.

1. **`ProviderResult` / `ProviderStatus`**
   *Current:* `list[str]` conflates availability, emptiness and content. *Problem:* DL-P0-002. *Target:* `{status, rows, error, query, window}`. *Migration:* change four `ObserveHub` methods and one `_runtime` mapping; the wire format of `EvidenceItem` is unchanged. *Compatibility:* full — stored job JSON is unaffected. *Risk:* low. *Rollback:* revert two files. *Verification:* the closed-port regression test in DL-P0-002.

2. **`EvidenceStatus` / `Severity` / `ConfidenceBand`** as three distinct types, with a model validator enforcing "no `SUPPORTED` without a successful retrieval".
   *Migration:* rename `Finding.confidence` → `Finding.status`, keeping `confidence` as a deprecated alias for one release so stored results still parse. *Risk:* medium (touches the UI). *Verification:* a test asserting the validator rejects a fabricated `SUPPORTED` item.

3. **`ToolCall`** — `{id, run_id, tool, arguments, started_at, finished_at, status, result_digest}`, persisted.
   *Migration:* one new table; wrap `ContextTools` methods in a recording context manager; no existing data changes. *Payoff:* delivers the real timeline, reproducibility, per-run cost accounting and the write audit trail simultaneously. **Highest-leverage item in the entire plan.**

4. **`Run`** distinct from `Job` — a Job is scheduling; a Run is an investigation with a workflow version, a capability snapshot, a configuration digest and a DevLens version.
   *Migration:* add columns to `jobs` and a `runs` table; old rows carry nulls and render as "provenance unavailable".

5. **`Proposal`** — what a workflow wants to do; an Approval references one. *Migration:* new table; `POST /approvals` gains a `proposal_id` and rejects free text after a deprecation window.

6. **`Finding.id`** so findings can be referenced, deduplicated and tracked across runs.

7. **Persisted `Evidence`** with provenance and a content hash, instead of a field inside a result blob.

### Phase C — Production investigation readiness
- Time ranges and `query_range` for Loki and Prometheus; record the window in every evidence item.
- Read-only credential requirement for the SQL gateway, documented and asserted at startup; function allowlist as defence in depth. *(DL-P1-013)*
- Per-upstream resilience policies; timeouts counted as failures. *(DL-P1-014)*
- Full pagination with disclosed caps. *(DL-P1-010)*
- Streamed, capped, killable subprocess execution. *(DL-P1-011)*
- Structured logging with request and run ids in the local app.
- A real isolation boundary for `--run-tests` (container with `--network=none --read-only --cap-drop=ALL --pids-limit`), gated behind a new `repository_code_execution` capability.

### Phase D — Write-back readiness
- Proposals produced by workflows; approvals referencing a proposal id and a payload hash. *(DL-P1-005)*
- Persisted approvals with an expiry; re-validation of the payload hash at execution time.
- Idempotency keyed on the approval id, reserved before the remote call, persisted. *(DL-P1-004)*
- Write payloads carrying the actual analysis; rename `push_commit`. *(DL-P1-006)*
- Audit entries at request, decision and execution, with the corrected redaction. *(DL-P2-019)*
- Resolve the real default branch per provider instead of assuming `main`.

### Phase E — Scale
Only when justified by measurement. `JobExecutor` seam first (before it is needed, because seams are cheap and migrations are not); a second executor implementation, Postgres and per-tenant isolation only when (a) you need more than one worker, (b) you exceed ~10 concurrent runs, or (c) you take a second tenant.

---

## 32. Target Architecture

Every proposed change is justified by a finding above. **No new infrastructure component is proposed** — no Kubernetes, no Kafka, no Temporal, no Postgres, no Redis, no Neo4j, no microservices. Every defect in this report is a code defect, and adding infrastructure would fix none of them.

```
  Browser ──► FastAPI ──► RunService ──► WorkflowRunner ──► Tool layer ──► Providers
                             │                │                 │
                             │                │                 └─► ToolCall recorded
                             │                └─► Proposals ──► ApprovalService ──► WriteGateway
                             └─► RunStore (SQLite) : Run · Step · ToolCall · Evidence ·
                                                     Hypothesis · Finding · Proposal · Approval
```

**Five changes, each with its justification.**

1. **`ProviderResult` at every external boundary.** *Justified by DL-P0-002.* Availability, emptiness and content stop being the same type. No new dependency.
2. **`ToolCall` persisted per provider call.** *Justified by DL-P1-009 (fake timeline), §39 (reproducibility), DL-P1-005 (no audit of what a run did).* One table, one context manager around `ContextTools`.
3. **`Proposal` between the workflow and the approval.** *Justified by DL-P1-004/005/006.* Approvals stop being free text.
4. **`JobExecutor` protocol with one in-process implementation.** *Justified by §21 — not by current pain, but because the seam is nearly free now and expensive later.* No behaviour change on day one.
5. **Consolidate the four capability tables into `CapabilityGuard`.** *Justified by DL-P2-018 and DL-P2-033.* Deletes code; adds nothing.

**Service catalog — the smallest first implementation (§64).** The architecture for this already exists: `catalog.services()` derives a catalog from the repository allowlist plus `services_affected` from completed runs, and the UI renders it. The right next step is **an explicit `[services]` block in `devlens.toml`**, not discovery:
```toml
[services.reply-delivery]
repository   = "your-org/reply-delivery"
language     = "python"
framework    = "fastapi"
dependencies = ["account-service", "media-store"]
databases    = ["replies-pg"]
queues       = ["replies.outbox"]
owners       = ["backend-core"]
```
`services()` then merges declared services with derived ones and marks each `declared` or `derived`. This is a day of work and it is what makes cross-repository investigation possible: given a ticket about reply delivery, DevLens can resolve which repositories, queues and databases are in scope and rank evidence across all of them instead of one. Automatic discovery from production should wait until the declared catalog is in daily use and its gaps are known — discovery built before the schema is proven will encode the wrong schema.

**Explicitly rejected and why.**

| Component | Verdict |
|---|---|
| Postgres | Rejected now. No measured contention; SQLite mis-configuration (WAL, indexes) accounts for the concurrency ceiling. Revisit at multi-worker or multi-tenant. |
| Redis / Celery / RQ / BullMQ | Rejected. No defect in this report is caused by in-process execution. |
| Temporal | Rejected. Durable execution solves a problem DevLens does not have; `recover_interrupted()` is the right-sized answer. |
| Kafka | Rejected. There is no event stream. |
| Neo4j | Rejected. The service graph has tens of nodes. |
| Kubernetes | Rejected. One container. |
| Microservices | Rejected. The monolith's boundaries are already clean. |
| A vector store / embeddings | Rejected for now. Fix tokenisation (DL-P2-022) first; measure whether lexical ranking is actually the limiting factor before adding a retrieval stack. |

**Missing features, by horizon (§66).**

| Horizon | Items |
|---|---|
| **Must have now** | Nothing new — Phase A is repair, not features |
| **Should have next** | Declared service catalog (above); real progress events; pagination; run provenance; proposals |
| **Later** | Cross-repository investigation; findings tracked across runs; report diffing between two commits; scheduled re-analysis |
| **Do not build yet** | Automatic service discovery; multi-tenancy in the local app; a job queue; code generation; a chat surface; a graph database; SSO/RBAC |

---

## 33. Production Readiness Scorecard

Scores are 0–10 and are not inflated; several strong areas score low because a single defect dominates the dimension.

| Dimension | Score | Reason | Top blocker |
|---|---:|---|---|
| Backend architecture | **7** | Clean acyclic layering, protocol-based providers, authorization at one boundary; `api.py` is a god module and capability enforcement is split across four tables | Consolidate the capability tables; split `api.py` |
| Frontend architecture | **5** | Sensible minimal stack, correct effect cleanup; two 300+ line grab-bag modules, duplicated polling, prop drilling, no tests | Extract a `useJob` hook; split `Catalog.tsx`/`Support.tsx` |
| API design | **6** | Consistent shapes, pydantic validation, coherent error translation; no pagination, no idempotency keys, wrong status codes for capacity and cancellation, `/docs` public | Pagination + correct status codes |
| Domain model | **4** | Good vocabulary (Evidence, Hypothesis, Finding, Unknowns); Run/Step/ToolCall/Proposal absent, two Evidence types, severity conflated with status, `REJECTED` missing | Phase B |
| Correctness | **4** | `ticket`/`analyze`/`review` are correct; `investigate` inverts evidence semantics, `design` is a template, pagination silently truncates | DL-P0-002 |
| Reliability | **3** | Timeouts and semaphores exist; no restart recovery, no multi-worker support, unkillable subprocesses, in-memory critical state | DL-P1-008 |
| Security | **4** | Deny-by-default, double-checked allowlists, no secret leakage to the browser, careful untrusted-response parsing; default-open, weak sessions, no rate limiting, incomplete redaction | DL-P1-012, DL-P2-020 |
| Sandbox safety | **1** | Path containment is correct; there is otherwise no isolation and all secrets are inherited (reproduced) | DL-P0-001 |
| Provider abstractions | **8** | Protocol + strategy + factory, genuinely swappable, each adapter owns its quirks; `main` and GitHub status vocabulary leak into Bitbucket | Provider-specific default branch |
| Jira integration | **6** | Strict URL validation, real ADF walker, custom fields, SHA-256 provenance, SSRF-safe attachments; comment truncation undisclosed, unbounded attachment fan-out | DL-P1-010 |
| GitHub integration | **7** | Rigorous SHA validation, refuses truncated trees, verifiably commit-pinned links; no pagination, no rate-limit handling, forks undistinguished | DL-P1-010 |
| Bitbucket integration | **6** | Best-in-repo `next`-link validation, correct default-branch resolution, correct auth modes; O(directories) tree walk, PR pagination ignored | DL-P2-021 |
| Deterministic mode | **6** | Genuinely independent of the LLM and verified so; excellent for ticket/analyze/review; `design` is a template and `investigate` is one hardcoded hypothesis | DL-P1-007 |
| LLM architecture | **5** | Five backends, OAuth including token refresh, SSE parsing, grounding system prompt, temperature 0; used in exactly one place, no cost controls, no token budget, no retries | Decide what the LLM is *for* |
| Evidence model | **3** | Right vocabulary, real provenance on lexical evidence, no fabricated percentages; status assigned from configuration rather than outcome | DL-P0-002 |
| Investigation workflow | **2** | Correct 3-pane shape; one hardcoded hypothesis, never uses the LLM, evidence semantics inverted | DL-P0-002 |
| Review workflow | **6** | Real heuristics with AST-based N+1 detection, acceptance-criterion cross-check, diff-anchored findings; N+1 findings can reference unchanged code, 25 speculative reads | Anchor all findings to the diff |
| Implementation workflow | **6** | Boundary is honest and clearly stated, sandbox branch never pushed; plan is one line per evidence path | DL-P0-001 |
| Observability readiness | **3** | Four providers implemented, capability-gated, per-provider error isolation is correct; no time ranges, no `query_range`, failures become evidence, DevLens emits no telemetry itself | DL-P0-002 |
| Testing | **5** | 175 tests in 10 s, real transport fixtures, isolated state, genuine security-boundary tests; 99.96% coverage caught none of the seventeen P0/P1 findings; no frontend or E2E tests | Behavioural tests for the findings above |
| DevOps | **3** | Non-root image, healthcheck, localhost binding, read-only workspace mount, named volume; **no CI at all**, UI not served, no resource limits, floating base tags | DL-P1-016 |
| Documentation accuracy | **6** | Genuinely disclaims six things most projects would overclaim; thirteen inaccurate claims including the primary Docker instruction | DL-P1-003 |
| UX | **7** | Real engineering information architecture, no chat window, disciplined epistemic copy; fake timeline, stub wizards, hardcoded capability claims | DL-P1-009 |
| Accessibility | **4** | Correct on the hardest requirement (status never colour-only), `:focus-visible`, reduced motion; no dialog semantics, no focus management, no live regions, light-mode contrast failures | DL-P2-035 |
| Maintainability | **6** | Small, readable, consistent, well-factored in the provider and policy layers; ~250 lines of dead code in security-relevant modules, two grab-bag frontend files | DL-P2-032 |
| Scalability readiness | **3** | Honest single-node design with semaphores and caps; single-worker-only with nothing enforcing it, SQLite mis-tuned, blocking calls in the event loop | DL-P1-011 |
| **Overall production readiness** | **3** | A correct, honest core analyzer surrounded by a product surface it does not yet support, with one credential-exfiltration path and one evidence-integrity inversion | DL-P0-001 and DL-P0-002 |

---

## 34. Final Recommendation

**Do not add another feature. Do six things, then reassess.**

DevLens has a real asset: a deterministic, bounded, source-linked ticket-and-repository analyzer that tells the truth about its own limits. That asset is currently obscured by a product surface — investigate, design, approvals, write-back, integrations, services, a multi-tenant mode — that was built ahead of the domain model needed to support it. The finding count in this report is not evidence of bad engineering; the provider layer, the policy boundary, the capability facade and the `ticket` workflow are all better than typical. It is evidence of **breadth outrunning depth**.

The two P0s are not stylistic. One hands every credential DevLens holds to any repository it is asked to test. The other makes DevLens *more* confident when the systems it is investigating are down — in a tool whose entire proposition is evidence-first reasoning. Both are small fixes. Both are reproducible in under ten lines.

**The specific order:**

1. **This week** — Phase A. Six items, none larger than a day: scrub the sandbox environment, fix the evidence-status mapping, serve the UI in Docker, tighten LLM auto-selection, recover interrupted jobs, and stand up CI. Take the twelve sub-hour quick wins in §30 alongside them.
2. **Next** — Phase B, and specifically **`ToolCall`**. It is the highest-leverage object in the plan: it makes the timeline real, makes runs reproducible, gives the audit log something worth auditing, and gives approvals something to reference. One table and a context manager.
3. **Then** — decide what the LLM is *for*. Today it writes one summary string, and that single use is the only thing standing between DevLens and a fully deterministic tool. Either commit to it (grounded synthesis over recorded evidence, with a trust boundary around retrieved content, a token budget and cost accounting) or remove it and market the determinism. The current halfway position carries the cost of both.
4. **Then** — rebuild `investigate` and `design` on the Phase-B model, or remove them until you can. Right now they are the two surfaces most likely to make an engineer distrust the whole product, and they sit next to the one surface most likely to earn trust.
5. **Only after Phase D** — enable write-back. The gate is sound; nothing meaningful passes through it yet.
6. **Do not build**: automatic service catalog discovery (ship the explicit TOML catalog first — it is a day of work and it is what unlocks cross-repository investigation), multi-tenancy in the local app, a job queue, Postgres, code generation, or any further UI surface.

**Is this a good foundation?** Yes — conditionally. The layering, the provider abstraction, the policy boundary and the deterministic default are the right bones, and they are worth building on for years. The condition is that the next increment goes into the domain model rather than into product surface. DevLens does not need more features. It needs `Run`, `ToolCall`, `Evidence` and `ProviderStatus` to be real objects, and then most of what it already claims will simply become true.
