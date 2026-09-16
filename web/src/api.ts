/**
 * API types and client.
 *
 * The three status scales the backend keeps separate stay separate here too:
 * `Severity` is how bad a finding is, `EvidenceStatus` is what the evidence
 * establishes, and `ConfidenceBand` is how sure DevLens is. They have different
 * colour ramps and are never rendered by the same component.
 */

export type Severity = "P0" | "P1" | "P2" | "P3";
export type EvidenceStatus =
  | "CONFIRMED"
  | "SUPPORTED"
  | "HYPOTHESIS"
  | "UNKNOWN"
  | "REJECTED";
export type ConfidenceBand = "LOW" | "MEDIUM" | "HIGH" | "VERY_HIGH";
export type RunState = "queued" | "running" | "completed" | "failed" | "cancelled";
export type ProviderStatus =
  | "AVAILABLE"
  | "EMPTY"
  | "UNAVAILABLE"
  | "DENIED"
  | "TIMEOUT"
  | "NOT_CONFIGURED";

export type Provenance = {
  retrieved: boolean;
  captured_at: number;
  source: string;
  provider?: string | null;
  provider_status?: ProviderStatus | null;
  repository?: string | null;
  commit?: string | null;
  ref?: string | null;
  path?: string | null;
  line?: number | null;
  url?: string | null;
  query?: string | null;
  window_start?: number | null;
  window_end?: number | null;
};

export type EvidenceItem = {
  id: string;
  type: string;
  source: string;
  observation: string;
  status: EvidenceStatus;
  excerpt?: string | null;
  selectors: string[];
  provenance: Provenance;
};

export type Hypothesis = {
  id: string;
  statement: string;
  status: "open" | "supported" | "rejected" | "confirmed";
  confidence: ConfidenceBand;
  supporting_evidence: string[];
  contradicting_evidence: string[];
  next_experiment?: string | null;
};

export type Finding = {
  id: string;
  severity: Severity;
  status: EvidenceStatus;
  confidence: ConfidenceBand;
  category: string;
  title: string;
  file?: string | null;
  line?: number | null;
  production_scenario?: string | null;
  evidence: string[];
  impact?: string | null;
  recommended_fix?: string | null;
  required_test?: string | null;
  verification?: string | null;
};

export type ProviderResult = {
  provider: string;
  status: ProviderStatus;
  rows: string[];
  error?: string | null;
  query?: string | null;
  truncated: boolean;
};

export type Truncation = {
  source: string;
  fetched: number;
  total?: number | null;
  reason: string;
};

export type ToolCall = {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  started_at: number;
  finished_at?: number | null;
  status: "running" | "ok" | "error" | "denied";
  error?: string | null;
  result_bytes: number;
};

export type RunRecord = {
  id: string;
  command: string;
  project: string;
  status: RunState;
  devlens_version: string;
  workflow_version: string;
  capabilities: string[];
  config_digest?: string | null;
  model_provider?: string | null;
  model_name?: string | null;
  repository?: string | null;
  commit?: string | null;
  requested_ref?: string | null;
  ticket_key?: string | null;
  started_at: number;
  finished_at?: number | null;
  tool_calls: ToolCall[];
};

export type Job = {
  id: string;
  run_id?: string | null;
  command: string;
  project: string;
  status: RunState;
  progress?: string | null;
  request: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  error?: string | null;
  error_kind?: string | null;
  created_at: number;
  finished_at?: number | null;
};

export type Project = {
  name: string;
  repositories: string[];
  jira_projects: string[];
  git_provider: string;
};

export type AuthStatus = {
  required: boolean;
  authenticated: boolean;
  session_seconds: number;
};

export type Capabilities = {
  enabled: string[];
  denied: string[];
  reasons: Record<string, string>;
  enable_with: Record<string, string>;
};

export type Proposal = {
  id: string;
  run_id: string;
  action: string;
  repository?: string | null;
  ticket_key?: string | null;
  target: string;
  payload: Record<string, unknown>;
  rationale: string;
  payload_sha256: string;
  created_at: number;
};

export type Approval = {
  id: string;
  proposal_id: string;
  run_id?: string | null;
  action: string;
  target: string;
  payload_sha256: string;
  status: "pending" | "approving" | "approved" | "rejected" | "expired";
  result?: string | null;
  created_at: number;
  expires_at: number;
  decided_at?: number | null;
};

export type Integration = {
  name: string;
  status: string;
  scope: string;
  configured_by: string;
};

export type Dashboard = {
  recent: Job[];
  counts: Record<string, number>;
  integrations: Integration[];
  capabilities: Capabilities;
};

export type ServiceItem = {
  id: string;
  name: string;
  repository?: string | null;
  provider?: string | null;
  project?: string | null;
  language?: string | null;
  framework?: string | null;
  dependencies: string[];
  databases: string[];
  queues: string[];
  owners: string[];
  findings: number;
  origin: string;
};

export type KnowledgeDoc = {
  id: string;
  category: string;
  title: string;
  body: string;
  services: string[];
  related_runs: string[];
};

export type SearchResult = {
  query: string;
  groups: Record<string, { title: string; href: string; kind: string }[]>;
};

export type Notice = {
  id: string;
  kind: string;
  title: string;
  body: string;
  href: string;
};

export type Capacity = {
  events_per_day: number;
  average_per_sec: number;
  peak_multiplier: number;
  peak_per_sec: number;
  payload_kb: number;
  daily_ingress_gb: number;
  method: string;
};

export type Route = { page: string; id: string; query: URLSearchParams };

export class ApiError extends Error {
  readonly status: number;
  readonly kind: string;
  constructor(message: string, status: number, kind: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
  }
}

async function parse(response: Response): Promise<unknown> {
  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!response.ok) {
    const record = (body ?? {}) as { detail?: unknown; kind?: unknown };
    const detail =
      typeof record.detail === "string" ? record.detail : response.statusText;
    const kind = typeof record.kind === "string" ? record.kind : "http_error";
    throw new ApiError(detail, response.status, kind);
  }
  return body;
}

const DEFAULT_TIMEOUT = 30_000;

async function send(path: string, init: RequestInit, timeout: number) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    return await parse(
      await fetch(path, {
        ...init,
        credentials: "include",
        signal: controller.signal,
      }),
    );
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError("The request timed out.", 504, "timeout");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  get: (path: string, timeout = DEFAULT_TIMEOUT) => send(path, {}, timeout),
  post: (path: string, body?: unknown, timeout = DEFAULT_TIMEOUT) =>
    send(
      path,
      {
        method: "POST",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      },
      timeout,
    ),
};

export function route(): Route {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path = "", search = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean);
  const aliases: Record<string, string> = {
    home: "overview",
    investigate: "investigations",
    review: "reviews",
    implement: "implementations",
    design: "designs",
  };
  const first = parts[0] ?? "";
  return {
    page: aliases[first] ?? first ?? "welcome",
    id: parts[1] ?? "",
    query: new URLSearchParams(search),
  };
}

export function go(path: string) {
  window.location.hash = path.startsWith("#") ? path.slice(1) : path;
}

/** Turn a typed API error into something an operator can act on. */
export function errorHint(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.kind) {
      case "policy_denied":
        return `Not allowed: ${error.message}`;
      case "capacity":
        return "DevLens is at capacity. Retry in a moment.";
      case "conflict":
        return error.message;
      case "timeout":
        return "The run exceeded its time budget.";
      case "provider_unavailable":
        return `A provider circuit is open: ${error.message}`;
      case "provider_error":
        return `A provider failed: ${error.message}`;
      case "invalid_input":
        return `Check the request: ${error.message}`;
      default:
        return error.status === 401 ? "Sign in required." : error.message;
    }
  }
  return error instanceof Error ? error.message : String(error);
}

export function intentPath(guess: {
  intent: string;
  ticket_key?: string | null;
  pr?: number | null;
  repository?: string | null;
  question?: string | null;
}): string {
  const params = new URLSearchParams();
  if (guess.question) params.set("question", guess.question);
  if (guess.ticket_key) params.set("ticket", guess.ticket_key);
  if (guess.repository) params.set("repository", guess.repository);
  if (guess.pr) params.set("pr", String(guess.pr));
  const suffix = params.toString() ? `?${params}` : "";
  switch (guess.intent) {
    case "DEBUG":
    case "PERFORMANCE":
    case "JIRA_ANALYSIS":
      return `/investigations${suffix}`;
    case "CODE_REVIEW":
      return `/reviews${suffix}`;
    case "IMPLEMENTATION":
      return `/implementations${suffix}`;
    case "SYSTEM_DESIGN":
      return `/designs${suffix}`;
    case "OBSERVABILITY":
      return `/observe${suffix}`;
    default:
      return `/ask${suffix}`;
  }
}

export function jobHref(job: Job): string {
  if (job.command === "review") return `/reviews/${job.id}`;
  if (job.command === "implement") return `/implementations/${job.id}`;
  if (job.command === "design") return `/designs/${job.id}`;
  if (job.command === "ask") return `/ask?run=${job.id}`;
  return `/investigations/${job.id}`;
}

export function duration(job: Job): string {
  const end = job.finished_at ?? Date.now() / 1000;
  const seconds = Math.max(0, Math.round(end - job.created_at));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function jobTitle(job: Job): string {
  const request = job.request ?? {};
  const result = (job.result ?? {}) as { task?: unknown };
  return String(
    request["ticket_key"] ||
      (request["pr"] ? `PR #${request["pr"]}` : "") ||
      request["question"] ||
      request["query"] ||
      result.task ||
      job.command,
  );
}

export const EVIDENCE_ICON: Record<string, string> = {
  code: "</>",
  git: "⑂",
  jira: "▤",
  image: "▦",
  log: "≡",
  trace: "⌁",
  sql: "▥",
  metric: "◫",
  test: "✓",
  benchmark: "◷",
  document: "☰",
};

export function has(capabilities: Capabilities | null, name: string): boolean {
  return Boolean(capabilities?.enabled.includes(name));
}

export function reasonFor(
  capabilities: Capabilities | null,
  name: string,
): string {
  return capabilities?.reasons[name] ?? "";
}
