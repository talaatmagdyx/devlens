import { FormEvent, useState } from "react";
import {
  Approval,
  Capabilities,
  Job,
  Proposal,
  ProviderResult,
  RunRecord,
  api,
  duration,
  errorHint,
  go,
  jobHref,
} from "../api";
import { useAsync, useJob } from "../hooks";
import {
  Empty,
  ErrorNote,
  Live,
  ProviderChip,
  RunStateChip,
} from "../ui";

/** Proposals a run produced, and the one-click path to requesting approval. */
export function ProposalList({
  proposals,
  capabilities,
}: {
  proposals: Proposal[];
  capabilities: Capabilities | null;
}) {
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const writesEnabled = capabilities?.enabled.includes("git_writeback") ?? false;

  if (proposals.length === 0) return null;

  async function request(proposal: Proposal) {
    setError("");
    try {
      await api.post("/approvals", { proposal_id: proposal.id });
      setMessage(
        writesEnabled
          ? `Queued for approval. Approving will perform ${proposal.action} on ${proposal.target}.`
          : `Queued for approval. Write-back is disabled, so approving records the decision without contacting the remote.`,
      );
    } catch (err) {
      setError(errorHint(err));
    }
  }

  return (
    <section className="card" style={{ marginTop: 16 }} aria-labelledby="proposals">
      <h3 id="proposals">Proposed actions</h3>
      <p className="lede">
        Each proposal carries its exact payload. Approving authorises that
        payload and nothing else.
      </p>
      <ErrorNote message={error} />
      {proposals.map((item) => (
        <article className="finding" key={item.id}>
          <div className="row">
            <strong>{item.action}</strong>
            <span className="meta">{item.target}</span>
          </div>
          <p>{item.rationale}</p>
          <details>
            <summary>Payload ({item.payload_sha256.slice(0, 12)})</summary>
            <pre className="excerpt">{JSON.stringify(item.payload, null, 2)}</pre>
          </details>
          <button className="primary" type="button" onClick={() => request(item)}>
            Request approval
          </button>
        </article>
      ))}
      <Live message={message} />
      {message && <p className="notice">{message}</p>}
    </section>
  );
}

export function ApprovalsPage({ capabilities }: { capabilities: Capabilities | null }) {
  const { data, error, reload } = useAsync<Approval[]>(
    () => api.get("/approvals") as Promise<Approval[]>,
    [],
  );
  const [actionError, setActionError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const writesEnabled =
    (capabilities?.enabled.includes("git_writeback") ?? false) ||
    (capabilities?.enabled.includes("jira_writeback") ?? false);

  async function decide(id: string, path: "approve" | "reject") {
    setActionError("");
    try {
      const record = (await api.post(`/approvals/${id}/${path}`)) as Approval;
      setAnnouncement(`Approval ${path}ed: ${record.result ?? record.status}`);
      reload();
    } catch (err) {
      setActionError(errorHint(err));
      reload();
    }
  }

  const items = data ?? [];
  return (
    <div>
      <h1>Approvals</h1>
      <p className="lede">
        {writesEnabled
          ? "Write-back is enabled. Approving executes the exact payload shown, once."
          : capabilities?.reasons["git_writeback"] ??
            "Write-back is disabled; approving records the decision only."}
      </p>
      <ErrorNote message={actionError || error} />
      {items.length === 0 ? (
        <Empty>
          <strong>Nothing waiting for you</strong>
          A write action — a Jira comment, a branch push, a pull request — is
          proposed here before it happens, with the exact payload it will send.
          Approving one is the only way DevLens changes anything outside itself.
        </Empty>
      ) : (
        items.map((item) => (
          <article className="card" key={item.id} style={{ marginBottom: 12 }}>
            <div className="row">
              <strong>{item.action}</strong>
              <RunStateChip value={item.status} />
              <span className="meta">
                expires {new Date(item.expires_at * 1000).toLocaleTimeString()}
              </span>
            </div>
            <p>{item.target}</p>
            <p className="meta">payload {item.payload_sha256.slice(0, 16)}</p>
            {item.result && <p className="notice">{item.result}</p>}
            {item.status === "pending" && (
              <div className="row">
                <button
                  className="ghost"
                  type="button"
                  onClick={() => decide(item.id, "reject")}
                >
                  Reject
                </button>
                <button
                  className="primary"
                  type="button"
                  onClick={() => decide(item.id, "approve")}
                >
                  Approve
                </button>
              </div>
            )}
          </article>
        ))
      )}
      <Live message={announcement} />
    </div>
  );
}

export function RunsPage({ id }: { id: string }) {
  if (id) return <RunDetail jobId={id} />;
  return <RunList />;
}

function RunList() {
  const { data, error } = useAsync<Job[]>(
    () => api.get("/jobs?limit=100") as Promise<Job[]>,
    [],
  );
  const jobs = data ?? [];
  return (
    <div>
      <h1>Runs</h1>
      <p className="lede">Every run, with the provenance needed to explain it later.</p>
      <ErrorNote message={error} />
      {jobs.length === 0 ? (
        <Empty>
          <strong>No runs yet</strong>
          Start one from the <a href="#/ask">Analyze</a> section. Every run is
          kept here with its commit, the providers it reached, and what it could
          not see.
        </Empty>
      ) : (
        <table className="table">
          <caption className="visually-hidden">All runs</caption>
          <thead>
            <tr>
              <th scope="col">Run</th>
              <th scope="col">Type</th>
              <th scope="col">Status</th>
              <th scope="col">Project</th>
              <th scope="col">Duration</th>
              <th scope="col">Started</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.id}>
                <td>
                  <a href={`#/runs/${job.id}`}>{job.id.slice(0, 8)}</a>
                </td>
                <td>
                  <a href={`#${jobHref(job)}`}>{job.command}</a>
                </td>
                <td>
                  <RunStateChip value={job.status} />
                </td>
                <td>{job.project}</td>
                <td className="meta">{duration(job)}</td>
                <td className="meta">
                  {new Date(job.created_at * 1000).toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function RunDetail({ jobId }: { jobId: string }) {
  const { job, events, error } = useJob(jobId);
  const { data: run } = useAsync<RunRecord | null>(
    () => api.get(`/jobs/${jobId}/run`).catch(() => null) as Promise<RunRecord | null>,
    [jobId],
  );

  if (error) return <ErrorNote message={error} />;
  if (!job) return <p>Opening run…</p>;
  const result = (job.result ?? {}) as Record<string, unknown>;

  return (
    <div className="card">
      <h1>{job.command}</h1>
      <div className="row">
        <RunStateChip value={job.status} />
        <span className="meta">{duration(job)}</span>
        <a className="ghost" href={`#${jobHref(job)}`}>
          Open workspace
        </a>
      </div>
      {job.error && <ErrorNote message={job.error} />}

      <h3>Provenance</h3>
      {run ? (
        <dl className="provenance">
          <dt>DevLens</dt>
          <dd>
            {run.devlens_version} · workflow {run.workflow_version}
          </dd>
          <dt>Capabilities at run time</dt>
          <dd>{run.capabilities.join(", ") || "none"}</dd>
          <dt>Model</dt>
          <dd>
            {run.model_provider ? `${run.model_provider} / ${run.model_name}` : "none"}
          </dd>
          <dt>Repository</dt>
          <dd>
            {run.repository ?? "—"} @ {run.commit?.slice(0, 12) ?? "—"} (requested{" "}
            {run.requested_ref ?? "—"})
          </dd>
          <dt>Configuration digest</dt>
          <dd>{run.config_digest?.slice(0, 16) ?? "—"}</dd>
        </dl>
      ) : (
        <Empty>No provenance recorded for this run.</Empty>
      )}

      <h3>Tool calls</h3>
      {(run?.tool_calls ?? []).length === 0 ? (
        <Empty>No tool calls recorded.</Empty>
      ) : (
        <table className="table">
          <caption className="visually-hidden">Tool calls in this run</caption>
          <thead>
            <tr>
              <th scope="col">Tool</th>
              <th scope="col">Status</th>
              <th scope="col">Duration</th>
              <th scope="col">Result size</th>
            </tr>
          </thead>
          <tbody>
            {(run?.tool_calls ?? []).map((call) => (
              <tr key={call.id}>
                <td>{call.tool}</td>
                <td>{call.status}</td>
                <td className="meta">
                  {call.finished_at
                    ? `${Math.round((call.finished_at - call.started_at) * 1000)} ms`
                    : "—"}
                </td>
                <td className="meta">{call.result_bytes}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Timeline</h3>
      {events.length === 0 ? (
        <Empty>No events recorded.</Empty>
      ) : (
        <ol className="timeline">
          {events.map((item) => (
            <li key={item.seq}>
              <span className="meta">{item.kind}</span> {item.message}
            </li>
          ))}
        </ol>
      )}

      <h3>Result</h3>
      <pre className="excerpt">{JSON.stringify(result, null, 2).slice(0, 20000)}</pre>
    </div>
  );
}

export function ObservePage({
  query,
  capabilities,
}: {
  query: URLSearchParams;
  capabilities: Capabilities | null;
}) {
  const [path, setPath] = useState(query.get("path") ?? "");
  const [error, setError] = useState("");
  const { data } = useAsync<Job[]>(
    () => api.get("/jobs?command=observe&limit=20") as Promise<Job[]>,
    [],
  );
  const live = capabilities?.enabled.includes("observability_provider") ?? false;
  const latest = (data ?? [])[0];
  const providers =
    ((latest?.result?.["provider_results"] as ProviderResult[] | undefined) ?? []);

  async function start(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const job = (await api.post("/jobs", {
        command: "observe",
        path: path || null,
      })) as Job;
      go(`/investigations/${job.id}`);
    } catch (err) {
      setError(errorHint(err));
    }
  }

  return (
    <div className="grid-2">
      <form className="card" onSubmit={start}>
        <h1>Observe</h1>
        <p className="lede">
          {live
            ? "Live providers are configured. Availability is reported per source."
            : capabilities?.reasons["observability_provider"] ??
              "No live provider is configured; this is a repository scan only."}
        </p>
        <ErrorNote message={error} />
        <label className="field" htmlFor="obs-path">
          <span>Local path</span>
          <input
            id="obs-path"
            value={path}
            onChange={(event) => setPath(event.target.value)}
          />
        </label>
        <button className="primary" type="submit">
          Scan workspace
        </button>
      </form>
      <section className="card" aria-labelledby="provider-status">
        <h2 id="provider-status">Provider status (last run)</h2>
        {providers.length === 0 ? (
          <Empty>
          <strong>No provider queried yet</strong>
          Observability is off until a Loki, Prometheus, Tempo or SQL endpoint is
          configured. Until then DevLens says UNKNOWN rather than guessing.
        </Empty>
        ) : (
          <ul className="files">
            {providers.map((item) => (
              <li key={item.provider}>
                <ProviderChip value={item.status} /> {item.provider}
                {item.error && <div className="meta">{item.error}</div>}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
