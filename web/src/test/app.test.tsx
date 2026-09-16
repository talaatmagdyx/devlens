/**
 * The application shell.
 *
 * What is asserted here is what a keyboard-only operator and a screen-reader
 * user actually need: a skip link, one labelled landmark per region, a sign-in
 * form that says where credentials live, navigation that marks the current page,
 * and a command palette reachable without a mouse.
 */

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { api } from "../api";

const RESPONSES: Record<string, unknown> = {
  "/auth/status": { required: false, authenticated: true, session_seconds: 3600 },
  "/projects": [
    { name: "default", repositories: ["org/repo"], jira_projects: ["DEV"], git_provider: "github" },
  ],
  "/capabilities": { enabled: [], denied: ["llm"], reasons: { llm: "off" }, enable_with: {} },
  "/notifications": [
    { id: "n1", kind: "run.completed", title: "ticket completed", body: "DEV-1", href: "/runs/1" },
  ],
};

function stubApi(overrides: Record<string, unknown> = {}) {
  const table = { ...RESPONSES, ...overrides };
  vi.spyOn(api, "get").mockImplementation(async (path: string) => {
    const key = Object.keys(table).find((candidate) => path.startsWith(candidate));
    if (key) return table[key];
    return [];
  });
}

describe("the shell", () => {
  beforeEach(() => {
    window.location.hash = "#/overview";
    stubApi();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    window.location.hash = "";
  });

  it("shows a loading state before auth is known", () => {
    vi.spyOn(api, "get").mockImplementation(() => new Promise(() => undefined));
    render(<App />);
    expect(screen.getByText(/Loading DevLens/)).toBeInTheDocument();
  });

  it("offers a skip link and labelled landmarks", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveAttribute("href", "#main");
    expect(document.getElementById("main")).not.toBeNull();
  });

  it("marks the current page for assistive technology", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
    const nav = screen.getByRole("navigation", { name: "Main" });
    const current = within(nav).getByRole("link", { current: "page" });
    expect(current).toHaveTextContent("Overview");
  });

  it("every icon-only control carries an accessible name", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
    for (const button of screen.getAllByRole("button")) {
      const name = button.getAttribute("aria-label") ?? button.textContent ?? "";
      expect(name.trim().length).toBeGreaterThan(1);
    }
  });

  it("toggles the sidebar and reports its state", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
    const toggle = screen.getByRole("button", { name: /Collapse sidebar/ });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    expect(
      screen.getByRole("button", { name: /Expand sidebar/ }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("shows notifications from the server, not invented ones", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Notifications/ }));
    const region = await screen.findByRole("region", { name: "Notifications" });
    expect(within(region).getByText("ticket completed")).toBeInTheDocument();
  });

  it("says when there is nothing to notify about", async () => {
    stubApi({ "/notifications": [] });
    render(<App />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Notifications/ }));
    expect(await screen.findByText("No notifications.")).toBeInTheDocument();
  });

  it("opens the command palette from the keyboard", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("navigates when the hash changes", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument());
    act(() => {
      window.location.hash = "#/approvals";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(await screen.findByRole("heading", { name: "Approvals" })).toBeInTheDocument();
  });
});

describe("sign-in", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.location.hash = "";
  });

  it("asks for a password and says where credentials live", async () => {
    window.location.hash = "#/overview";
    stubApi({ "/auth/status": { required: true, authenticated: false, session_seconds: 0 } });
    render(<App />);
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByText(/never sent to the browser/)).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
  });

  it("reports a rejected password without echoing it", async () => {
    window.location.hash = "#/overview";
    stubApi({ "/auth/status": { required: true, authenticated: false, session_seconds: 0 } });
    const { ApiError } = await import("../api");
    vi.spyOn(api, "post").mockRejectedValue(new ApiError("Invalid password.", 403, "policy_denied"));
    render(<App />);

    await userEvent.type(await screen.findByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent(/Invalid password/);
    expect(error).not.toHaveTextContent("hunter2");
  });

  it("enters the application once the password is accepted", async () => {
    window.location.hash = "#/overview";
    stubApi({ "/auth/status": { required: true, authenticated: false, session_seconds: 0 } });
    vi.spyOn(api, "post").mockResolvedValue({ required: true, authenticated: true });
    render(<App />);

    await userEvent.type(await screen.findByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument(),
    );
  });

  it("assumes a sign-in is required when the status call fails", async () => {
    window.location.hash = "#/overview";
    vi.spyOn(api, "get").mockRejectedValue(new Error("network"));
    render(<App />);
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
  });
});
