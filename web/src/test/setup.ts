import "@testing-library/jest-dom/vitest";

// jsdom has no EventSource; the useJob hook degrades to polling without it.
class StubEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  close() {}
  addEventListener() {}
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).EventSource = StubEventSource;
