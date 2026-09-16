import { FormEvent, useState } from "react";
import {
  Dashboard,
  Finding,
  Job,
  api,
  duration,
  errorHint,
  go,
  intentPath,
  jobHref,
  jobTitle,
} from "../api";
import { useAsync } from "../hooks";
import {
  ConfidenceChip,
  Empty,
  ErrorNote,
  RunStateChip,
  SeverityChip,
  StatusChip,
} from "../ui";

/** The six things DevLens can be asked to do, in the order an operator meets them. */
const COMMANDS: [string, string, string][] = [
  ["#/ask", "Ask", "A question about code, a ticket or production. Every claim traces to an evidence card."],
  ["#/investigations", "Investigate", "Classify a symptom, gather evidence, rank hypotheses against it."],
  ["#/reviews", "Review", "Deterministic heuristics anchored to the lines a pull request changed."],
  ["#/implementations", "Implement", "Plan a change. DevLens proposes; it does not write application code."],
  ["#/designs", "Design", "A worksheet with arithmetic, not invented numbers."],
  ["#/observe", "Observe", "Find the gaps in what production can tell you."],
];

export function OverviewPage() {
  const { data, error, reload } = useAsync<Dashboard>(
    () => api.get("/dashboard") as Promise<Dashboard>,
    [],
  );
  const [text, setText] = useState("");
  const [routeError, setRouteError] = useState("");

  async function classify(event: FormEvent) {
    event.preventDefault();
    setRouteError("");
    try {
      const guess = (await api.post("/intent", { text })) as Parameters<
        typeof intentPath
      >[0];
      go(intentPath(guess));
    } catch (err) {
      setRouteError(errorHint(err));
    }
  }

  const recent = data?.recent ?? [];
  const active = recent.filter(
    (job) => job.status === "queued" || job.status === "running",
  );
  const findings = collectFindings(recent);
  const denied = data?.capabilities?.denied ?? [];

  const completed = recent.filter((job) => job.status === "completed").length;
  const enabled = data?.capabilities?.enabled ?? [];

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Workspace</h1>
          <p className="lede">
            Paste a ticket, a pull request, or a production question. DevLens
            routes it to the workflow that can answer it with evidence.
          </p>
        </div>
      </div>
      <form className="universal" onSubmit={classify}>
        <label className="visually-hidden" htmlFor="universal-input">
          Ask about code, Jira, a pull request, or production
        </label>
        <input
          id="universal-input"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Ask about code, Jira, a PR, or production…"
        />
        <button className="primary" type="submit">
          Route
        </button>
      </form>
      <ErrorNote message={routeError || error} />

      <h2 id="start">Start a run</h2>
      <div className="cards" aria-labelledby="start">
        {COMMANDS.map(([href, title, blurb]) => (
          <a className="quick" href={href} key={href}>
            <strong>{title}</strong>
            <p>{blurb}</p>
          </a>
        ))}
      </div>

      <dl className="stats" style={{ marginTop: 22 }}>
        <div className="stat">
          <dt>Active</dt>
          <dd>{active.length}</dd>
        </div>
        <div className="stat">
          <dt>Completed</dt>
          <dd>{completed}</dd>
        </div>
        <div className="stat">
          <dt>Findings</dt>
          <dd>{findings.length}</dd>
        </div>
        <div className="stat">
          <dt>Capabilities on</dt>
          <dd>
            {enabled.length}
            <small> / {enabled.length + denied.length}</small>
          </dd>
        </div>
      </dl>

      {active.length > 0 && (
        <section aria-labelledby="active-runs">
          <h2 id="active-runs">In flight</h2>
          <ul className="files">
            {active.map((job) => (
              <li key={job.id}>
                <a href={`#${jobHref(job)}`}>
                  {jobTitle(job)} · {job.command}
                </a>{" "}
                <RunStateChip value={job.status} />
              </li>
            ))}
          </ul>
        </section>
      )}

      {findings.length > 0 && (
        <section aria-labelledby="recent-findings">
          <h2 id="recent-findings">Recent findings</h2>
          {findings.slice(0, 8).map((item) => (
            <article className="finding" key={`${item.id}-${item.job}`}>
              <div className="row">
                <SeverityChip value={item.severity} />
                <StatusChip value={item.status} />
                <ConfidenceChip value={item.confidence} />
              </div>
              <strong>{item.title}</strong>
              <div className="meta">{item.file ?? item.job}</div>
            </article>
          ))}
        </section>
      )}

      <section aria-labelledby="recent-work">
        <div className="page-head" style={{ marginBottom: 8 }}>
          <h2 id="recent-work" style={{ margin: 0 }}>Recent work</h2>
          <button className="ghost" type="button" onClick={reload}>
            Refresh
          </button>
        </div>
        {recent.length === 0 ? (
          <Empty>
            <strong>No runs yet</strong>
            Every run DevLens completes is listed here with the provenance needed
            to explain it later — the commit, the providers it reached, and what
            it could not see.
          </Empty>
        ) : (
          <div className="scroller">
          <table className="table">
            <caption className="visually-hidden">Recent DevLens runs</caption>
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Type</th>
                <th scope="col">Status</th>
                <th scope="col">Duration</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((job) => (
                <tr key={job.id}>
                  <td>
                    <a href={`#${jobHref(job)}`}>{jobTitle(job)}</a>
                  </td>
                  <td>{job.command}</td>
                  <td>
                    <RunStateChip value={job.status} />
                  </td>
                  <td className="meta">{duration(job)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>

      {denied.length > 0 && (
        <section aria-labelledby="capabilities">
          <h2 id="capabilities">Switched off</h2>
          <p className="lede">
            What DevLens will not do in this deployment, and the variable that
            would change that. Nothing here fails silently.
          </p>
          <div className="scroller">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Capability</th>
                  <th scope="col">Consequence</th>
                  <th scope="col">Enable with</th>
                </tr>
              </thead>
              <tbody>
                {denied.map((name) => (
                  <tr key={name}>
                    <td>
                      <code>{name}</code>
                    </td>
                    <td>{data?.capabilities.reasons[name]}</td>
                    <td>
                      <code className="meta">
                        {data?.capabilities.enable_with[name]}
                      </code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

function collectFindings(jobs: Job[]): (Finding & { job: string })[] {
  return jobs.flatMap((job) => {
    const findings = (job.result?.["findings"] as Finding[] | undefined) ?? [];
    return findings
      .filter((item) => typeof item === "object" && item !== null)
      .map((item) => ({ ...item, job: jobTitle(job) }));
  });
}
