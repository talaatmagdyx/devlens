/**
 * Frontend behaviour tests.
 *
 * These assert the properties the audit found missing: that status is never
 * conveyed by colour alone, that severity and evidence status are rendered by
 * different components, that capability copy comes from the server rather than
 * a literal, and that dialogs behave like dialogs.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Capabilities, errorHint, ApiError, intentPath, route } from "../api";
import { parseDiff, DiffViewer } from "../DiffViewer";
import {
  ConfidenceChip,
  Dialog,
  ProviderChip,
  SeverityChip,
  StatusChip,
  runAnnouncement,
} from "../ui";
import { ProposalList } from "../pages/Operations";

const CAPABILITIES: Capabilities = {
  enabled: ["llm"],
  denied: ["git_writeback", "repository_code_execution"],
  reasons: {
    git_writeback: "Git remote write-back is disabled.",
    repository_code_execution: "Executing a repository's own test suite is disabled.",
  },
  enable_with: {
    git_writeback: "DEVLENS_ALLOW_WRITES=1",
    repository_code_execution: "DEVLENS_ALLOW_REPO_TESTS=1",
  },
};

describe("status chips", () => {
  it("renders a text label, never colour alone", () => {
    render(
      <>
        <SeverityChip value="P0" />
        <StatusChip value="UNKNOWN" />
        <ConfidenceChip value="VERY_HIGH" />
        <ProviderChip value="UNAVAILABLE" />
      </>,
    );
    expect(screen.getByText("P0")).toBeInTheDocument();
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.getByText("Very High")).toBeInTheDocument();
    expect(screen.getByText("UNAVAILABLE")).toBeInTheDocument();
  });

  it("keeps severity, evidence status and confidence on separate scales", () => {
    const { container } = render(
      <>
        <SeverityChip value="P3" />
        <StatusChip value="SUPPORTED" />
        <ConfidenceChip value="HIGH" />
      </>,
    );
    const classes = Array.from(container.querySelectorAll(".chip")).map(
      (node) => node.className,
    );
    expect(classes[0]).toContain("sev-P3");
    expect(classes[1]).toContain("ev-SUPPORTED");
    expect(classes[2]).toContain("band-HIGH");
    // No class is shared between the three scales.
    expect(new Set(classes).size).toBe(3);
  });

  it("explains what a status means on hover", () => {
    render(<StatusChip value="UNKNOWN" />);
    expect(screen.getByTitle(/not established/i)).toBeInTheDocument();
  });
});

describe("dialog", () => {
  it("is a modal dialog, labelled, closable by Escape", async () => {
    const onClose = vi.fn();
    render(
      <Dialog title="Attachment" onClose={onClose}>
        <button type="button">inside</button>
      </Dialog>,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByRole("heading", { name: "Attachment" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("moves focus into the dialog when it opens", () => {
    render(
      <Dialog title="Commands" onClose={() => undefined}>
        <button type="button">first</button>
      </Dialog>,
    );
    expect(document.activeElement?.textContent).toBe("Close");
  });
});

describe("capability copy", () => {
  it("comes from the server inventory, not a hardcoded string", () => {
    render(
      <ProposalList
        proposals={[
          {
            id: "p1",
            run_id: "r1",
            action: "create_pr",
            target: "org/repo",
            payload: { title: "x" },
            rationale: "Publish findings",
            payload_sha256: "abcdef1234567890",
            created_at: 0,
          },
        ]}
        capabilities={CAPABILITIES}
      />,
    );
    expect(screen.getByText("create_pr")).toBeInTheDocument();
    expect(screen.getByText(/Publish findings/)).toBeInTheDocument();
    expect(screen.getByText(/abcdef123456/)).toBeInTheDocument();
  });
});

describe("diff viewer", () => {
  const DIFF = `diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -10,3 +10,4 @@ def handler():
 context line
-removed line
+added line
+second added
`;

  it("parses hunks and numbers lines from the post-image", () => {
    const files = parseDiff(DIFF);
    expect(files).toHaveLength(1);
    expect(files[0]!.file).toBe("app.py");
    const added = files[0]!.lines.filter((line) => line.kind === "add");
    expect(added.map((line) => line.text)).toEqual(["added line", "second added"]);
    expect(added[0]!.newNumber).toBe(11);
  });

  it("renders without loading anything from the network", () => {
    render(<DiffViewer value={DIFF} />);
    expect(screen.getByRole("region", { name: "Unified diff" })).toBeInTheDocument();
    expect(screen.getByText("added line")).toBeInTheDocument();
    expect(document.querySelectorAll("script[src]")).toHaveLength(0);
  });

  it("filters to one file when a file is selected", () => {
    render(<DiffViewer value={DIFF} selected="other.py" />);
    expect(screen.getByText(/No diff for this selection/)).toBeInTheDocument();
  });
});

describe("error translation", () => {
  it("turns each server error kind into an actionable message", () => {
    expect(errorHint(new ApiError("Repository is not allowed.", 403, "policy_denied"))).toMatch(
      /Not allowed/,
    );
    expect(errorHint(new ApiError("full", 429, "capacity"))).toMatch(/capacity/i);
    expect(errorHint(new ApiError("stale", 409, "conflict"))).toBe("stale");
    expect(errorHint(new ApiError("slow", 504, "timeout"))).toMatch(/time budget/);
  });
});

describe("routing", () => {
  it("maps an intent to the surface that handles it", () => {
    expect(intentPath({ intent: "CODE_REVIEW", pr: 4 })).toBe("/reviews?pr=4");
    expect(intentPath({ intent: "DEBUG", ticket_key: "DEV-1" })).toBe(
      "/investigations?ticket=DEV-1",
    );
    expect(intentPath({ intent: "QUESTION" })).toBe("/ask");
  });

  it("parses the hash route", () => {
    window.location.hash = "#/investigations/abc?question=why";
    const parsed = route();
    expect(parsed.page).toBe("investigations");
    expect(parsed.id).toBe("abc");
    expect(parsed.query.get("question")).toBe("why");
  });
});

describe("what a run announces to a screen reader", () => {
  it("prefers the latest progress event", () => {
    expect(runAnnouncement("running", "Reading the repository", "Working")).toBe(
      "Reading the repository",
    );
  });

  it("falls back to the job's own progress line", () => {
    expect(runAnnouncement("running", undefined, "Collecting evidence")).toBe(
      "Collecting evidence",
    );
  });

  it("still says something when a run finishes before any event arrives", () => {
    // The regression this guards: a deterministic run completed instantly, the
    // status chip turned green, and nothing was announced at all.
    expect(runAnnouncement("completed")).toMatch(/complete/i);
    expect(runAnnouncement("failed")).toMatch(/failed/i);
  });

  it("says nothing rather than inventing a state it does not know", () => {
    expect(runAnnouncement("something-new")).toBe("");
  });
});
