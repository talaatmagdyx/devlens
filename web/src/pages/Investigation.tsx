import { FormEvent, useState } from "react";
import {
  Capabilities,
  EVIDENCE_ICON,
  EvidenceItem,
  Finding,
  Hypothesis,
  Job,
  Project,
  ProviderResult,
  Truncation,
  api,
  duration,
  errorHint,
  go,
  jobTitle,
} from "../api";
import { useAsync, useJob } from "../hooks";
import {
  ConfidenceChip,
  Dialog,
  Empty,
  ErrorNote,
  Live,
  ProviderChip,
  RunStateChip,
  SeverityChip,
  StatusChip,
  runAnnouncement,
} from "../ui";

export function InvestigationsPage({
  id,
  query,
  projects,
  capabilities,
}: {
  id: string;
  query: URLSearchParams;
  projects: Project[];
  capabilities: Capabilities | null;
}) {
  if (id) return <InvestigationWorkspace jobId={id} capabilities={capabilities} />;
  return <InvestigationStart projects={projects} query={query} />;
}

function InvestigationStart({
  projects,
  query,
}: {
  projects: Project[];
  query: URLSearchParams;
}) {
  const { data, error } = useAsync<Job[]>(
    () => api.get("/jobs?limit=100") as Promise<Job[]>,
    [],
  );
  const [question, setQuestion] = useState(query.get("question") ?? "");
  const [ticket, setTicket] = useState(query.get("ticket") ?? "");
  const [path, setPath] = useState(query.get("path") ?? "");
  const [project, setProject] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("all");

  const chosen = project || projects[0]?.name || "default";
  const jobs = (data ?? []).filter((job) =>
    ["investigate", "ticket", "observe", "analyze"].includes(job.command),
  );

  async function start(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setSubmitError("");
    try {
      const job = (await api.post("/jobs", {
        command: "investigate",
        project: chosen,
        question: question || null,
        ticket_key: ticket || null,
        path: path || null,
        idempotency_key: `inv-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
      })) as Job;
      go(`/investigations/${job.id}`);
    } catch (err) {
      setSubmitError(errorHint(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid-2">
      <form className="card" onSubmit={start}>
        <h1>Investigate</h1>
        <p className="lede">
          DevLens classifies the symptom, collects evidence, and ranks candidate
          mechanisms. Nothing is confirmed without runtime evidence.
        </p>
        <ErrorNote message={submitError} />
        <label className="field" htmlFor="inv-question">
          <span>Question or symptom</span>
          <textarea
            id="inv-question"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Why did checkout p99 latency triple after Tuesday's deploy?"
          />
        </label>
        <label className="field" htmlFor="inv-ticket">
          <span>Jira key (optional)</span>
          <input
            id="inv-ticket"
            value={ticket}
            onChange={(event) => setTicket(event.target.value)}
            placeholder="DEV-1427"
          />
        </label>
        <label className="field" htmlFor="inv-path">
          <span>Local repository path (optional)</span>
          <input
            id="inv-path"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="/path/to/checkout"
          />
        </label>
        <label className="field" htmlFor="inv-project">
          <span>Project</span>
          <select
            id="inv-project"
            value={chosen}
            onChange={(event) => setProject(event.target.value)}
          >
            {(projects.length ? projects.map((item) => item.name) : ["default"]).map(
              (name) => (
                <option key={name}>{name}</option>
              ),
            )}
          </select>
        </label>
        <button className="primary" disabled={busy} type="submit">
          {busy ? "Starting…" : "Start investigation"}
        </button>
      </form>

      <section className="card" aria-labelledby="investigations">
        <h2 id="investigations">Investigations</h2>
        <ErrorNote message={error} />
        <div className="row" style={{ marginBottom: 12 }}>
          {["all", "running", "completed", "failed", "cancelled"].map((item) => (
            <button
              key={item}
              className={`ghost ${filter === item ? "primary" : ""}`}
              type="button"
              aria-pressed={filter === item}
              onClick={() => setFilter(item)}
            >
              {item}
            </button>
          ))}
        </div>
        {jobs.length === 0 ? (
          <Empty>
          <strong>No investigations yet</strong>
          Describe a symptom above. DevLens classifies it, gathers evidence, and
          ranks hypotheses against that evidence — it does not name a root cause
          without confirming it.
        </Empty>
        ) : (
          <table className="table">
            <caption className="visually-hidden">Previous investigations</caption>
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Title</th>
                <th scope="col">Top hypothesis</th>
                <th scope="col">Findings</th>
                <th scope="col">Duration</th>
              </tr>
            </thead>
            <tbody>
              {jobs
                .filter(
                  (job) =>
                    filter === "all" ||
                    job.status === filter ||
                    (filter === "running" && job.status === "queued"),
                )
                .map((job) => {
                  const top = (job.result?.["hypotheses"] as Hypothesis[] | undefined)?.[0];
                  return (
                    <tr key={job.id}>
                      <td>
                        <RunStateChip value={job.status} />
                      </td>
                      <td>
                        <a href={`#/investigations/${job.id}`}>{jobTitle(job)}</a>
                      </td>
                      <td>
                        {top ? (
                          <ConfidenceChip value={top.confidence} />
                        ) : (
                          <span className="meta">—</span>
                        )}
                      </td>
                      <td>
                        {((job.result?.["findings"] as unknown[] | undefined) ?? []).length}
                      </td>
                      <td className="meta">{duration(job)}</td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function InvestigationWorkspace({
  jobId,
  capabilities,
}: {
  jobId: string;
  capabilities: Capabilities | null;
}) {
  const { job, events, error, live } = useJob(jobId);
  const [tab, setTab] = useState("Overview");
  const [left, setLeft] = useState(true);
  const [right, setRight] = useState(true);
  const [stopError, setStopError] = useState("");

  async function stop() {
    try {
      await api.post(`/jobs/${jobId}/cancel`);
    } catch (err) {
      setStopError(errorHint(err));
    }
  }

  if (error) return <ErrorNote message={error} />;
  if (!job) return <p>Reading investigation…</p>;

  const result = (job.result ?? {}) as Record<string, unknown>;
  const evidence = (result["evidence_items"] as EvidenceItem[] | undefined) ?? [];
  const hypotheses = (result["hypotheses"] as Hypothesis[] | undefined) ?? [];
  const providers = (result["provider_results"] as ProviderResult[] | undefined) ?? [];
  const findings = (result["findings"] as Finding[] | undefined) ?? [];
  const truncations = (result["truncations"] as Truncation[] | undefined) ?? [];
  const ticket = result["ticket"] as Record<string, unknown> | undefined;
  const rootCause = result["root_cause"] as string | undefined;
  const rootStatus = (result["root_cause_status"] as string | undefined) ?? "UNKNOWN";
  const running = job.status === "queued" || job.status === "running";
  const panes = ["workspace-3", left ? "" : "no-left", right ? "" : "no-right"]
    .filter(Boolean)
    .join(" ");

  return (
    <div>
      <header className="header-run">
        <div>
          <h1>{jobTitle(job)}</h1>
          <p className="lede">
            {String(result["executive_summary"] ?? job.progress ?? "Collecting context.")}
          </p>
          <div className="row">
            <RunStateChip value={job.status} />
            <span className="meta">{duration(job)}</span>
            {job.run_id && (
              <a className="meta" href={`#/runs/${job.id}`}>
                run {job.run_id.slice(0, 8)}
              </a>
            )}
            {running && (
              <span className="meta">{live ? "live" : "reconnecting…"}</span>
            )}
          </div>
        </div>
        <div className="row">
          <button
            className="ghost"
            type="button"
            aria-pressed={left}
            onClick={() => setLeft((value) => !value)}
          >
            Context
          </button>
          <button
            className="ghost"
            type="button"
            aria-pressed={right}
            onClick={() => setRight((value) => !value)}
          >
            Evidence
          </button>
          {running && (
            <button className="ghost" type="button" onClick={stop}>
              Stop
            </button>
          )}
          {job.result && (
            <a className="ghost" href={`/jobs/${job.id}/export?format=md`}>
              Export
            </a>
          )}
        </div>
      </header>
      <ErrorNote message={stopError} />
      {job.error && (
        <p className="error" role="alert">
          {job.error}
          <br />
          Hypotheses that needed this source stay unverified.
        </p>
      )}

      <div className={panes}>
        {left && (
          <aside className="pane" aria-label="Context">
            <div className="tabs" role="tablist">
              {["Overview", "Jira", "Providers", "Not inspected"].map((item) => (
                <button
                  key={item}
                  role="tab"
                  aria-selected={tab === item}
                  className={`tab ${tab === item ? "active" : ""}`}
                  type="button"
                  onClick={() => setTab(item)}
                >
                  {item}
                </button>
              ))}
            </div>
            {tab === "Overview" && (
              <div>
                <h3>Investigated</h3>
                <ul>
                  {((result["investigated"] as string[] | undefined) ?? []).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
                <h3>Unknowns</h3>
                <ul>
                  {((result["unknowns"] as string[] | undefined) ?? []).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {tab === "Jira" &&
              (ticket ? (
                <div>
                  <strong>{String(ticket["key"])}</strong>
                  <p>{String(ticket["summary"] ?? "")}</p>
                  <div className="meta">
                    {String(ticket["priority"] ?? "")} · {String(ticket["status"] ?? "")}
                  </div>
                  <AttachmentPanel ticket={ticket} capabilities={capabilities} />
                </div>
              ) : (
                <Empty>No Jira context on this run.</Empty>
              ))}
            {tab === "Providers" &&
              (providers.length === 0 ? (
                <Empty>No runtime provider was queried.</Empty>
              ) : (
                <ul className="files">
                  {providers.map((item) => (
                    <li key={item.provider}>
                      <ProviderChip value={item.status} /> {item.provider}
                      {item.error && <div className="meta">{item.error}</div>}
                    </li>
                  ))}
                </ul>
              ))}
            {tab === "Not inspected" &&
              (truncations.length === 0 ? (
                <Empty>Nothing was truncated on this run.</Empty>
              ) : (
                <ul>
                  {truncations.map((item) => (
                    <li key={item.source}>
                      {item.source}: inspected {item.fetched}
                      {item.total ? ` of ${item.total}` : ""} ({item.reason})
                    </li>
                  ))}
                </ul>
              ))}
          </aside>
        )}

        <section className="pane main" aria-label="Investigation">
          <h2>Timeline</h2>
          {events.length === 0 ? (
            <Empty>
              {running ? "Waiting for the first tool call…" : "No events recorded."}
            </Empty>
          ) : (
            <ol className="timeline">
              {events.map((item) => (
                <li key={item.seq}>
                  <span className="meta">{readable(item.kind)}</span> {item.message}
                </li>
              ))}
            </ol>
          )}
          <Live
            message={runAnnouncement(
              job.status,
              events[events.length - 1]?.message,
              job.progress,
            )}
          />

          <h2>Hypotheses</h2>
          {hypotheses.length === 0 ? (
            <Empty>Hypotheses appear once the symptom has been classified.</Empty>
          ) : (
            hypotheses.map((item) => (
              <article className="finding" key={item.id}>
                <div className="row">
                  <strong>{item.id}</strong>
                  <span className={`chip hyp hyp-${item.status}`}>
                    <span className="glyph" aria-hidden="true">
                      ◈
                    </span>
                    {item.status}
                  </span>
                  <ConfidenceChip value={item.confidence} />
                </div>
                <p>{item.statement}</p>
                <div className="meta">
                  Supporting {item.supporting_evidence.length} · Contradicting{" "}
                  {item.contradicting_evidence.length}
                </div>
                {item.next_experiment && (
                  <p className="next">Next experiment: {item.next_experiment}</p>
                )}
              </article>
            ))
          )}

          {!running && (
            <section className="root-cause">
              <div className="ask-step">Root cause</div>
              <h2>
                {rootCause ??
                  "Not established. No runtime evidence confirmed a mechanism."}
              </h2>
              <StatusChip value={rootStatus as never} />
              <p className="meta">
                DevLens confirms a root cause only when runtime evidence
                establishes it. Code and screenshots show intent, not behaviour.
              </p>
            </section>
          )}

          {findings.length > 0 && (
            <>
              <h2>Findings</h2>
              {findings.map((item) => (
                <article className="finding" key={item.id}>
                  <div className="row">
                    <SeverityChip value={item.severity} />
                    <StatusChip value={item.status} />
                    <ConfidenceChip value={item.confidence} />
                  </div>
                  <strong>{item.title}</strong>
                  {item.impact && <p>{item.impact}</p>}
                  {item.recommended_fix && <p>Fix: {item.recommended_fix}</p>}
                </article>
              ))}
            </>
          )}
        </section>

        {right && (
          <aside className="pane" aria-label="Evidence">
            <h2>Evidence</h2>
            {evidence.length === 0 ? (
              <Empty>Evidence cards appear as tools complete.</Empty>
            ) : (
              evidence.map((item) => (
                <article className="evidence" key={item.id}>
                  <div className="row">
                    <span className="meta">
                      {EVIDENCE_ICON[item.type] ?? "☰"} {item.id}
                    </span>
                    <StatusChip value={item.status} />
                  </div>
                  <strong>{item.source}</strong>
                  <p>{item.observation}</p>
                  {item.excerpt && <pre className="excerpt">{item.excerpt}</pre>}
                  {item.provenance.url && (
                    <a
                      className="meta"
                      href={item.provenance.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      source
                    </a>
                  )}
                </article>
              ))
            )}
          </aside>
        )}
      </div>
    </div>
  );
}

function AttachmentPanel({
  ticket,
  capabilities,
}: {
  ticket: Record<string, unknown>;
  capabilities: Capabilities | null;
}) {
  const [open, setOpen] = useState<Record<string, unknown> | null>(null);
  const items = (ticket["attachments"] as Record<string, unknown>[] | undefined) ?? [];
  const vision = capabilities?.enabled.includes("screenshot_analysis") ?? false;
  if (items.length === 0) return <Empty>No attachments on this ticket.</Empty>;
  return (
    <div>
      <h3>Attachments</h3>
      {items.map((item) => (
        <button
          key={String(item["id"])}
          className="ghost"
          type="button"
          onClick={() => setOpen(item)}
        >
          {String(item["filename"] ?? item["id"])}
        </button>
      ))}
      {open && (
        <Dialog
          title={String(open["filename"])}
          onClose={() => setOpen(null)}
          labelledBy="attachment-title"
        >
          <p className="meta">
            {vision
              ? "Screenshot analysis is enabled. It produces observations and can never confirm a root cause."
              : capabilities?.reasons["screenshot_analysis"] ??
                "Screenshot analysis is disabled; this is metadata only."}
          </p>
          <StatusChip value="UNKNOWN" />
          <ul>
            {((open["observations"] as string[] | undefined) ?? []).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          {Boolean(open["injection_suspected"]) && (
            <p className="error">
              This attachment contains instruction-like text. DevLens treats it as
              data and never passes it to a model as an instruction.
            </p>
          )}
        </Dialog>
      )}
    </div>
  );
}

function readable(kind: string): string {
  const map: Record<string, string> = {
    RunStarted: "Started",
    ToolStarted: "Calling",
    ToolCompleted: "Completed",
    ToolFailed: "Failed",
    ObservationCreated: "Observed",
    HypothesisCreated: "Hypothesis",
    SymptomClassified: "Classified",
    RunCompleted: "Finished",
    RunFinished: "Finished",
    RunFailed: "Failed",
  };
  return map[kind] ?? kind;
}
