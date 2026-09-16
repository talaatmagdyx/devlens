/**
 * Shared hooks.
 *
 * `useJob` replaces the four near-identical polling blocks that used to live in
 * each workspace component, and it consumes the real server-sent event stream
 * rather than polling alongside it: events arrive as work happens, with a
 * sequence cursor so a reconnect neither repeats nor drops anything.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, Job, api, errorHint } from "./api";

export type StreamEvent = {
  seq: number;
  ts: number;
  kind: string;
  message: string;
  data: Record<string, unknown>;
};

export type JobView = {
  job: Job | null;
  events: StreamEvent[];
  error: string;
  live: boolean;
  reload: () => void;
};

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function useJob(jobId: string | null): JobView {
  const [job, setJob] = useState<Job | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [error, setError] = useState("");
  const [live, setLive] = useState(false);
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let source: EventSource | null = null;
    let timer: number | undefined;

    const load = async () => {
      try {
        const next = (await api.get(`/jobs/${jobId}`)) as Job;
        if (cancelled) return;
        setJob(next);
        setError("");
        if (!TERMINAL.has(next.status)) {
          // A slow fallback in case the event stream cannot be established;
          // the stream, not this, is the primary signal.
          timer = window.setTimeout(load, 5000);
        }
      } catch (err) {
        if (!cancelled) setError(errorHint(err));
      }
    };

    void load();
    try {
      source = new EventSource(`/jobs/${jobId}/events`, { withCredentials: true });
      source.onopen = () => !cancelled && setLive(true);
      source.onerror = () => !cancelled && setLive(false);
      source.onmessage = (event) => {
        if (cancelled) return;
        try {
          const body = JSON.parse(event.data) as StreamEvent;
          setEvents((current) =>
            current.some((item) => item.seq === body.seq)
              ? current
              : [...current, body],
          );
          if (body.kind === "RunCompleted" || body.kind === "RunFinished") void load();
        } catch {
          /* a malformed frame is ignored rather than breaking the stream */
        }
      };
      for (const kind of ["RunFinished", "RunCompleted", "RunFailed"]) {
        source.addEventListener(kind, () => {
          if (!cancelled) void load();
        });
      }
    } catch {
      setLive(false);
    }

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      source?.close();
    };
  }, [jobId, nonce]);

  return useMemo(
    () => ({ job, events, error, live, reload }),
    [job, events, error, live, reload],
  );
}

export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[],
): { data: T | null; error: string; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loader()
      .then((value) => {
        if (cancelled) return;
        setData(value);
        setError("");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof ApiError && err.status === 401
            ? "Sign in required."
            : errorHint(err),
        );
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, error, loading, reload };
}

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/** Trap focus inside a dialog and restore it to the opener on close. */
export function useFocusTrap(
  active: boolean,
  onClose: () => void,
): React.RefObject<HTMLDivElement | null> {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!active) return;
    const opener = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const focusable = () =>
      Array.from(node?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    focusable()[0]?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (items.length === 0) return;
      const first = items[0]!;
      const last = items[items.length - 1]!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      // Hand focus back to whatever opened the dialog. When that was the
      // document itself — the palette opens from a keyboard shortcut, so it
      // usually is — restoring it puts focus on <body>, and the next Tab
      // starts again at the top of the page. Land on the main landmark
      // instead, which is where the reader was.
      const target =
        opener && opener !== document.body && opener.isConnected
          ? opener
          : document.getElementById("main");
      target?.focus?.();
    };
  }, [active, onClose]);

  return ref;
}

/** Announce changes to assistive technology without stealing focus. */
export function useAnnouncer(): [string, (message: string) => void] {
  const [message, setMessage] = useState("");
  const announce = useCallback((next: string) => {
    setMessage("");
    window.setTimeout(() => setMessage(next), 50);
  }, []);
  return [message, announce];
}
