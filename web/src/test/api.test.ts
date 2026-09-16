/**
 * The API client and the pure helpers around it.
 *
 * These are the functions the whole UI depends on for correctness: routing,
 * error translation, and the fetch wrapper that has to turn a server error
 * into something an operator can act on rather than "Failed to fetch".
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  Capabilities,
  Job,
  api,
  duration,
  errorHint,
  go,
  has,
  intentPath,
  jobHref,
  jobTitle,
  reasonFor,
  route,
} from "../api";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    run_id: "run-1",
    command: "ticket",
    project: "default",
    status: "completed",
    progress: "completed",
    request: { ticket_key: "DEV-1" },
    result: null,
    error: null,
    error_kind: null,
    created_at: 1_000,
    finished_at: 1_075,
    ...overrides,
  } as Job;
}

const CAPABILITIES: Capabilities = {
  enabled: ["llm"],
  denied: ["git_writeback"],
  reasons: { git_writeback: "Git remote write-back is disabled." },
  enable_with: { git_writeback: "DEVLENS_ALLOW_WRITES=1" },
};

describe("the fetch wrapper", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sends credentials so the session cookie travels", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("{}", { status: 200 }));
    await api.get("/jobs");
    expect(vi.mocked(fetch).mock.calls[0]![1]).toMatchObject({
      credentials: "include",
    });
  });

  it("sends a JSON content type only when there is a body", async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response("{}", { status: 200 }));
    await api.post("/approvals/x/approve");
    const [, first] = vi.mocked(fetch).mock.calls[0]!;
    expect(first?.headers).toBeUndefined();

    await api.post("/jobs", { command: "ask" });
    const [, second] = vi.mocked(fetch).mock.calls[1]!;
    expect(second?.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(second?.body).toBe(JSON.stringify({ command: "ask" }));
  });

  it("turns a typed error body into an ApiError carrying the kind", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "Repository is not allowed.", kind: "policy_denied" }), {
        status: 403,
      }),
    );
    await expect(api.get("/jobs")).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      kind: "policy_denied",
      message: "Repository is not allowed.",
    });
  });

  it("falls back to the status text when the body is not JSON", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response("<html>gateway</html>", { status: 502, statusText: "Bad Gateway" }),
    );
    await expect(api.get("/jobs")).rejects.toMatchObject({
      status: 502,
      kind: "http_error",
    });
  });

  it("reports a timeout as a timeout rather than an abort", async () => {
    vi.mocked(fetch).mockRejectedValue(
      new DOMException("aborted", "AbortError"),
    );
    await expect(api.get("/jobs", 10)).rejects.toMatchObject({ kind: "timeout" });
  });

  it("returns null for an empty successful body", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response("", { status: 200 }));
    await expect(api.get("/jobs")).resolves.toBeNull();
  });
});

describe("error translation", () => {
  it("gives every server error kind an actionable sentence", () => {
    const cases: Array<[ApiError, RegExp]> = [
      [new ApiError("Repository is not allowed.", 403, "policy_denied"), /^Not allowed/],
      [new ApiError("full", 429, "capacity"), /at capacity/],
      [new ApiError("stale", 409, "conflict"), /^stale$/],
      [new ApiError("slow", 504, "timeout"), /time budget/],
      [new ApiError("open", 503, "provider_unavailable"), /circuit is open/],
      [new ApiError("down", 502, "provider_error"), /provider failed/],
      [new ApiError("bad", 422, "invalid_input"), /Check the request/],
    ];
    for (const [error, pattern] of cases) {
      expect(errorHint(error)).toMatch(pattern);
    }
  });

  it("asks the operator to sign in on a 401", () => {
    expect(errorHint(new ApiError("nope", 401, "http_error"))).toBe("Sign in required.");
  });

  it("handles a plain error and a non-error alike", () => {
    expect(errorHint(new Error("boom"))).toBe("boom");
    expect(errorHint("boom")).toBe("boom");
  });
});

describe("routing", () => {
  afterEach(() => {
    window.location.hash = "";
  });

  it("defaults to the welcome surface", () => {
    window.location.hash = "";
    expect(route().page).toBe("");
  });

  it("resolves aliases so old links keep working", () => {
    for (const [alias, page] of [
      ["home", "overview"],
      ["investigate", "investigations"],
      ["review", "reviews"],
      ["implement", "implementations"],
      ["design", "designs"],
    ]) {
      window.location.hash = `#/${alias}/abc`;
      expect(route().page).toBe(page);
      expect(route().id).toBe("abc");
    }
  });

  it("parses the query string", () => {
    window.location.hash = "#/investigations?question=why&ticket=DEV-1";
    const parsed = route();
    expect(parsed.query.get("question")).toBe("why");
    expect(parsed.query.get("ticket")).toBe("DEV-1");
  });

  it("navigates by setting the hash", () => {
    go("/reviews?pr=4");
    expect(window.location.hash).toBe("#/reviews?pr=4");
    go("#/ask");
    expect(window.location.hash).toBe("#/ask");
  });
});

describe("intent routing", () => {
  it("sends each intent to the surface that handles it", () => {
    expect(intentPath({ intent: "DEBUG", ticket_key: "DEV-1" })).toBe(
      "/investigations?ticket=DEV-1",
    );
    expect(intentPath({ intent: "PERFORMANCE" })).toBe("/investigations");
    expect(intentPath({ intent: "CODE_REVIEW", pr: 4 })).toBe("/reviews?pr=4");
    expect(intentPath({ intent: "IMPLEMENTATION", ticket_key: "DEV-2" })).toBe(
      "/implementations?ticket=DEV-2",
    );
    expect(intentPath({ intent: "SYSTEM_DESIGN", question: "a feed" })).toBe(
      "/designs?question=a+feed",
    );
    expect(intentPath({ intent: "OBSERVABILITY" })).toBe("/observe");
    expect(intentPath({ intent: "QUESTION" })).toBe("/ask");
  });
});

describe("job presentation", () => {
  it("links each command to its workspace", () => {
    expect(jobHref(job({ command: "review" }))).toBe("/reviews/job-1");
    expect(jobHref(job({ command: "implement" }))).toBe("/implementations/job-1");
    expect(jobHref(job({ command: "design" }))).toBe("/designs/job-1");
    expect(jobHref(job({ command: "ask" }))).toBe("/ask?run=job-1");
    expect(jobHref(job({ command: "ticket" }))).toBe("/investigations/job-1");
  });

  it("names a run by what was asked, not by its identifier", () => {
    expect(jobTitle(job())).toBe("DEV-1");
    expect(jobTitle(job({ request: { pr: 7 } }))).toBe("PR #7");
    expect(jobTitle(job({ request: { question: "why?" } }))).toBe("why?");
    expect(jobTitle(job({ request: {}, command: "observe" }))).toBe("observe");
  });

  it("formats a duration in minutes and seconds", () => {
    expect(duration(job())).toBe("1m 15s");
    expect(duration(job({ finished_at: 1_030 }))).toBe("30s");
  });

  it("never shows a negative duration for a clock skew", () => {
    expect(duration(job({ finished_at: 900 }))).toBe("0s");
  });
});

describe("capability helpers", () => {
  it("reports what is on and why the rest is off", () => {
    expect(has(CAPABILITIES, "llm")).toBe(true);
    expect(has(CAPABILITIES, "git_writeback")).toBe(false);
    expect(has(null, "llm")).toBe(false);
    expect(reasonFor(CAPABILITIES, "git_writeback")).toMatch(/disabled/);
    expect(reasonFor(CAPABILITIES, "llm")).toBe("");
    expect(reasonFor(null, "llm")).toBe("");
  });
});
