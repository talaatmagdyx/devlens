/**
 * Hooks.
 *
 * `useJob` is the one that matters: it consumes the server-sent event stream so
 * the timeline appears while the run is happening rather than after it, keeps a
 * slow poll as a fallback when the stream cannot be established, and never
 * duplicates an event on reconnect.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAsync, useFocusTrap, useJob } from "../hooks";
import { ApiError, api } from "../api";

type Listener = (event: MessageEvent) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: Listener | null = null;
  closed = false;
  readonly listeners = new Map<string, Listener[]>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(kind: string, listener: Listener) {
    this.listeners.set(kind, [...(this.listeners.get(kind) ?? []), listener]);
  }

  close() {
    this.closed = true;
  }

  emit(payload: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }

  fire(kind: string) {
    for (const listener of this.listeners.get(kind) ?? []) {
      listener({ data: "{}" } as MessageEvent);
    }
  }
}

function JobView({ id }: { id: string }) {
  const { job, events, error, live } = useJob(id);
  return (
    <div>
      <p data-testid="status">{job?.status ?? "…"}</p>
      <p data-testid="live">{live ? "live" : "not live"}</p>
      <p data-testid="error">{error}</p>
      <ol>
        {events.map((item) => (
          <li key={item.seq}>{item.message}</li>
        ))}
      </ol>
    </div>
  );
}

describe("useJob", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("opens the stream for the job and reports that it is live", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ id: "job-1", status: "running" });
    render(<JobView id="job-1" />);
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("running"));

    const source = FakeEventSource.instances[0]!;
    expect(source.url).toBe("/jobs/job-1/events");
    act(() => source.onopen?.());
    await waitFor(() => expect(screen.getByTestId("live")).toHaveTextContent("live"));
  });

  it("renders events as they arrive, in order, without duplicates", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ id: "job-1", status: "running" });
    render(<JobView id="job-1" />);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const source = FakeEventSource.instances[0]!;

    act(() => {
      source.emit({ seq: 1, ts: 0, kind: "Step", message: "first", data: {} });
      source.emit({ seq: 2, ts: 0, kind: "Step", message: "second", data: {} });
      source.emit({ seq: 1, ts: 0, kind: "Step", message: "first", data: {} });
    });

    await waitFor(() => {
      const items = screen.getAllByRole("listitem").map((node) => node.textContent);
      expect(items).toEqual(["first", "second"]);
    });
  });

  it("re-reads the job when the run finishes", async () => {
    const get = vi
      .spyOn(api, "get")
      .mockResolvedValueOnce({ id: "job-1", status: "running" })
      .mockResolvedValue({ id: "job-1", status: "completed" });
    render(<JobView id="job-1" />);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    act(() => FakeEventSource.instances[0]!.fire("RunFinished"));
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("completed"));
    expect(get.mock.calls.length).toBeGreaterThan(1);
  });

  it("ignores a malformed frame rather than breaking the stream", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ id: "job-1", status: "running" });
    render(<JobView id="job-1" />);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const source = FakeEventSource.instances[0]!;
    act(() => {
      source.onmessage?.({ data: "not json" } as MessageEvent);
      source.emit({ seq: 1, ts: 0, kind: "Step", message: "survived", data: {} });
    });
    await waitFor(() => expect(screen.getByText("survived")).toBeInTheDocument());
  });

  it("surfaces a load failure as an operator-facing message", async () => {
    vi.spyOn(api, "get").mockRejectedValue(
      new ApiError("Repository is not allowed.", 403, "policy_denied"),
    );
    render(<JobView id="job-1" />);
    await waitFor(() =>
      expect(screen.getByTestId("error")).toHaveTextContent("Not allowed"),
    );
  });

  it("closes the stream when the component goes away", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ id: "job-1", status: "running" });
    const view = render(<JobView id="job-1" />);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    view.unmount();
    expect(FakeEventSource.instances[0]!.closed).toBe(true);
  });

  it("does nothing at all without a job id", () => {
    render(<JobView id="" />);
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});

function AsyncView({ loader }: { loader: () => Promise<string> }) {
  const { data, error, loading, reload } = useAsync(loader, []);
  return (
    <div>
      <p data-testid="state">{loading ? "loading" : (error || data)}</p>
      <button type="button" onClick={reload}>
        reload
      </button>
    </div>
  );
}

describe("useAsync", () => {
  it("shows loading, then the value", async () => {
    render(<AsyncView loader={() => Promise.resolve("ok")} />);
    expect(screen.getByTestId("state")).toHaveTextContent("loading");
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("ok"));
  });

  it("translates a 401 into a sign-in prompt", async () => {
    render(
      <AsyncView
        loader={() => Promise.reject(new ApiError("nope", 401, "http_error"))}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("state")).toHaveTextContent("Sign in required."),
    );
  });

  it("reloads on demand", async () => {
    let calls = 0;
    const loader = () => Promise.resolve(`call ${++calls}`);
    render(<AsyncView loader={loader} />);
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("call 1"));
    await userEvent.click(screen.getByRole("button", { name: "reload" }));
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("call 2"));
  });
});

function TrapView({ onClose }: { onClose: () => void }) {
  const ref = useFocusTrap(true, onClose);
  return (
    <div ref={ref}>
      <button type="button">first</button>
      <button type="button">last</button>
    </div>
  );
}

describe("useFocusTrap", () => {
  it("focuses the first control and cycles with Tab", async () => {
    render(<TrapView onClose={() => undefined} />);
    expect(document.activeElement?.textContent).toBe("first");
    await userEvent.tab();
    expect(document.activeElement?.textContent).toBe("last");
    await userEvent.tab();
    expect(document.activeElement?.textContent).toBe("first");
  });

  it("cycles backwards with Shift+Tab", async () => {
    render(<TrapView onClose={() => undefined} />);
    await userEvent.tab({ shift: true });
    expect(document.activeElement?.textContent).toBe("last");
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    render(<TrapView onClose={onClose} />);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
  });
});
