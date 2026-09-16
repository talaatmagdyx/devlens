/**
 * Workspace surfaces.
 *
 * The audit's UI findings were all of one kind: the interface asserted more
 * than the run had established — a confident answer while evidence was still
 * UNKNOWN, a provider shown as healthy because it was configured, colour as the
 * only carrier of status. These tests pin the honest version of each surface.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Capabilities, Job, api } from "../api";
import { AskPage } from "../pages/Ask";
import { InvestigationsPage } from "../pages/Investigation";
import { ObservePage } from "../pages/Operations";
import { OverviewPage } from "../pages/Overview";
import { IntegrationsPage } from "../pages/Settings";

class SilentEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  addEventListener() {}
  close() {}
}

const NO_CAPABILITIES: Capabilities = {
  enabled: [],
  denied: ["observability_provider", "llm"],
  reasons: {
    observability_provider:
      "No observability provider is configured. Logs, metrics, traces and SELECT queries are unavailable.",
    llm: "No language model is configured. DevLens stays deterministic.",
  },
  enable_with: { observability_provider: "DEVLENS_LOKI_URL", llm: "DEVLENS_LLM_PROVIDER" },
};

function job(result: Record<string, unknown>, overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    run_id: "run-1",
    command: "investigate",
    project: "default",
    status: "completed",
    progress: "completed",
    request: { question: "why is checkout slow?" },
    result,
    error: null,
    error_kind: null,
    created_at: 0,
    finished_at: 10,
    ...overrides,
  } as Job;
}

beforeEach(() => {
  vi.stubGlobal("EventSource", SilentEventSource);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Ask", () => {
  it("frames the flow as question, sources, answer — not a chat", async () => {
    render(<AskPage query={new URLSearchParams()} projects={[]} />);
    expect(screen.getByRole("heading", { name: "Ask" })).toBeInTheDocument();
    expect(screen.getByText(/This is not a chat thread/)).toBeInTheDocument();
    expect(screen.getByLabelText("Question")).toBeRequired();
  });

  it("reports a refusal instead of navigating away", async () => {
    const { ApiError } = await import("../api");
    vi.spyOn(api, "post").mockRejectedValue(
      new ApiError("Analyze path is outside the configured workspace root.", 403, "policy_denied"),
    );
    render(<AskPage query={new URLSearchParams()} projects={[]} />);
    await userEvent.type(screen.getByLabelText("Question"), "why?");
    await userEvent.click(screen.getByRole("button", { name: "Ask DevLens" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/workspace root/);
  });

  it("shows the sources it inspected and says so when there were none", async () => {
    vi.spyOn(api, "get").mockResolvedValue(
      job({
        executive_summary: "Nothing is confirmed.",
        evidence_items: [],
        facts: [],
        unknowns: ["Loki is unreachable."],
      }),
    );
    render(<AskPage query={new URLSearchParams("run=job-1")} projects={[]} />);
    expect(await screen.findByText("0 inspected")).toBeInTheDocument();
    expect(screen.getByText("Nothing was inspected.")).toBeInTheDocument();
    expect(screen.getByText("Loki is unreachable.")).toBeInTheDocument();
    expect(screen.getByText(/not a hidden chain of thought/)).toBeInTheDocument();
  });

  it("renders each source with its evidence status", async () => {
    vi.spyOn(api, "get").mockResolvedValue(
      job({
        executive_summary: "A pool is saturated.",
        evidence_items: [
          {
            id: "E-1",
            type: "metric",
            source: "Prometheus",
            observation: "p99 tracks pool saturation",
            status: "SUPPORTED",
            selectors: [],
            provenance: { retrieved: true, captured_at: 0, source: "Prometheus" },
          },
        ],
        provider_results: [
          { provider: "Loki", status: "UNAVAILABLE", rows: [], truncated: false },
        ],
        facts: ["Queried Prometheus over a one-hour window."],
      }),
    );
    render(<AskPage query={new URLSearchParams("run=job-1")} projects={[]} />);
    expect(await screen.findByText("Prometheus")).toBeInTheDocument();
    expect(screen.getByText("SUPPORTED")).toBeInTheDocument();
    expect(screen.getByText("UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText(/one-hour window/)).toBeInTheDocument();
  });
});

describe("Investigate", () => {
  it("shows hypotheses with their own confidence, never a percentage", async () => {
    vi.spyOn(api, "get").mockResolvedValue(
      job({
        executive_summary: "Loki could not be reached.",
        root_cause: null,
        root_cause_status: "UNKNOWN",
        hypotheses: [
          {
            id: "H1",
            statement: "A connection pool is saturated.",
            status: "open",
            confidence: "LOW",
            supporting_evidence: [],
            contradicting_evidence: [],
            next_experiment: "Chart pool wait time against p99.",
          },
        ],
        evidence_items: [],
        unknowns: ["Loki is unreachable."],
      }),
    );
    render(
      <InvestigationsPage id="job-1" query={new URLSearchParams()} projects={[]} capabilities={NO_CAPABILITIES} />,
    );
    expect(await screen.findByText("A connection pool is saturated.")).toBeInTheDocument();
    expect(screen.getByText(/Chart pool wait time/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\d{1,3}%\s*confiden/i);
  });

  it("does not present a root cause when none was established", async () => {
    vi.spyOn(api, "get").mockResolvedValue(
      job({
        executive_summary: "Loki, Prometheus could not be reached.",
        root_cause: null,
        root_cause_status: "UNKNOWN",
        hypotheses: [],
        evidence_items: [],
        unknowns: ["Loki is unreachable."],
      }),
    );
    render(
      <InvestigationsPage id="job-1" query={new URLSearchParams()} projects={[]} capabilities={NO_CAPABILITIES} />,
    );
    expect(await screen.findByText(/could not be reached/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Root cause$/ })).toBeNull();
  });
});

describe("Observe", () => {
  it("says a provider is not configured rather than implying it is healthy", async () => {
    vi.spyOn(api, "get").mockResolvedValue([]);
    render(<ObservePage query={new URLSearchParams()} capabilities={NO_CAPABILITIES} />);
    expect(
      await screen.findByText(/No observability provider is configured/),
    ).toBeInTheDocument();
    // The zero-state names what is missing and what it would take to fix it,
    // rather than implying the providers are simply quiet.
    expect(screen.getByText("No provider queried yet")).toBeInTheDocument();
    expect(screen.getByText(/says UNKNOWN rather than guessing/)).toBeInTheDocument();
  });

  it("reports per-provider availability from the last run", async () => {
    vi.spyOn(api, "get").mockResolvedValue([
      job(
        {
          provider_results: [
            { provider: "Loki", status: "UNAVAILABLE", rows: [], error: "connection refused", truncated: false },
            { provider: "Prometheus", status: "AVAILABLE", rows: ["x"], truncated: false },
          ],
        },
        { command: "observe" },
      ),
    ]);
    render(<ObservePage query={new URLSearchParams()} capabilities={NO_CAPABILITIES} />);
    expect(await screen.findByText("UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText("AVAILABLE")).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
  });
});

describe("Integrations", () => {
  it("describes configuration state, and how it is configured", async () => {
    vi.spyOn(api, "get").mockResolvedValue([
      {
        name: "loki",
        status: "not_configured",
        scope: "read",
        configured_by: "environment variables; DevLens has no UI connect flow",
      },
    ]);
    render(<IntegrationsPage />);
    expect(await screen.findByText("loki")).toBeInTheDocument();
    expect(screen.getByText(/no UI connect flow/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).toBeNull();
  });
});

describe("Overview", () => {
  it("summarises real runs and stays empty when there are none", async () => {
    vi.spyOn(api, "get").mockImplementation(async (path: string) => {
      if (path.startsWith("/dashboard")) {
        return {
          recent: [],
          counts: {},
          integrations: [],
          capabilities: { enabled: [], denied: [], reasons: {}, enable_with: {} },
        };
      }
      return [];
    });
    render(<OverviewPage />);
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument());
    expect(document.body.textContent).not.toMatch(/\bLorem\b|placeholder/i);
  });
});
