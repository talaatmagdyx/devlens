/**
 * Shared presentation components.
 *
 * Severity, evidence status and confidence each get their own component and
 * their own colour ramp, because they answer different questions. Every chip
 * renders its label as text next to the dot, so status never depends on colour
 * alone.
 */

import { ReactNode } from "react";
import {
  ConfidenceBand,
  EvidenceStatus,
  ProviderStatus,
  RunState,
  Severity,
} from "./api";
import { useFocusTrap } from "./hooks";

export function Logo() {
  return (
    <svg className="logo" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <path
        d="M16 3L28 12v8L16 29 4 20v-8L16 3z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <circle cx="16" cy="4" r="1.6" fill="currentColor" />
    </svg>
  );
}

const SEVERITY_LABEL: Record<Severity, string> = {
  P0: "P0 critical",
  P1: "P1 high",
  P2: "P2 medium",
  P3: "P3 low",
};

export function SeverityChip({ value }: { value?: Severity | null }) {
  const key = (value ?? "P3") as Severity;
  return (
    <span className={`chip sev sev-${key}`} title={SEVERITY_LABEL[key] ?? key}>
      <span className="glyph" aria-hidden="true">
        ■
      </span>
      {key}
    </span>
  );
}

const STATUS_HELP: Record<EvidenceStatus, string> = {
  CONFIRMED: "Runtime evidence establishes this.",
  SUPPORTED: "Evidence was retrieved and points this way.",
  HYPOTHESIS: "Plausible, but not yet supported by retrieved evidence.",
  UNKNOWN: "Not established. A source was missing, unreachable or empty.",
  REJECTED: "Evidence contradicts this.",
};

export function StatusChip({ value }: { value?: EvidenceStatus | null }) {
  const key = (value ?? "UNKNOWN") as EvidenceStatus;
  return (
    <span className={`chip ev ev-${key}`} title={STATUS_HELP[key] ?? key}>
      <span className="glyph" aria-hidden="true">
        ●
      </span>
      {key}
    </span>
  );
}

const BAND_LABEL: Record<ConfidenceBand, string> = {
  LOW: "Low",
  MEDIUM: "Medium",
  HIGH: "High",
  VERY_HIGH: "Very High",
};

export function ConfidenceChip({ value }: { value?: ConfidenceBand | null }) {
  const key = (value ?? "LOW") as ConfidenceBand;
  return (
    <span className={`chip band band-${key}`} title={`Confidence: ${BAND_LABEL[key]}`}>
      <span className="glyph" aria-hidden="true">
        ▮
      </span>
      {BAND_LABEL[key] ?? key}
    </span>
  );
}

export function RunStateChip({ value }: { value: RunState | string }) {
  return (
    <span className={`chip run run-${value}`}>
      <span className="glyph" aria-hidden="true">
        ◆
      </span>
      {value}
    </span>
  );
}

const PROVIDER_HELP: Record<ProviderStatus, string> = {
  AVAILABLE: "Answered with data.",
  EMPTY: "Answered with no matching data. That is not evidence of absence.",
  UNAVAILABLE: "Could not be reached.",
  DENIED: "Refused the query.",
  TIMEOUT: "Did not answer within the time budget.",
  NOT_CONFIGURED: "No credentials configured.",
};

export function ProviderChip({ value }: { value: ProviderStatus }) {
  return (
    <span className={`chip prov prov-${value}`} title={PROVIDER_HELP[value]}>
      <span className="glyph" aria-hidden="true">
        ◇
      </span>
      {value}
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function ErrorNote({ message }: { message: string }) {
  if (!message) return null;
  return (
    <p className="error" role="alert">
      {message}
    </p>
  );
}

export function Live({ message }: { message: string }) {
  return (
    <p className="visually-hidden" role="status" aria-live="polite">
      {message}
    </p>
  );
}

/** What a run is doing, for anyone who cannot see the status chip change.
 *
 * The progress events carry the detail, but a run that finishes before the
 * first event arrives — a cached or deterministic one does — used to announce
 * nothing at all: the chip turned green in silence. The status is the floor.
 */
export function runAnnouncement(
  status: string,
  lastEvent?: string,
  progress?: string | null,
): string {
  if (lastEvent) return lastEvent;
  if (progress) return progress;
  return (
    {
      queued: "Queued.",
      running: "Running.",
      completed: "Run complete. The answer is below.",
      failed: "The run failed. The reason is below.",
      cancelled: "The run was cancelled.",
    }[status] ?? ""
  );
}

export function Dialog({
  title,
  onClose,
  children,
  labelledBy = "dialog-title",
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  labelledBy?: string;
}) {
  const ref = useFocusTrap(true, onClose);
  return (
    <div className="scrim" onMouseDown={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        ref={ref}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-head">
          <h2 id={labelledBy}>{title}</h2>
          <button className="ghost" type="button" onClick={onClose}>
            Close
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function greeting(): string {
  const hour = new Date().getHours();
  const when = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  return `${when}, operator`;
}

export function Capability({
  enabled,
  reason,
  children,
}: {
  enabled: boolean;
  reason: string;
  children: ReactNode;
}) {
  /** Render capability copy from the server's inventory, never from a literal. */
  return enabled ? <>{children}</> : <p className="meta">{reason}</p>;
}
