import { FormEvent, useState } from "react";
import { Capacity, Job, Project, api, errorHint, go, jobTitle } from "../api";
import { useAsync, useJob } from "../hooks";
import { Empty, ErrorNote, RunStateChip } from "../ui";

export type { Capacity };

export function DesignsPage({
  id,
  query,
  projects,
}: {
  id: string;
  query: URLSearchParams;
  projects: Project[];
}) {
  if (id) return <DesignWorkspace jobId={id} />;
  return <DesignStart query={query} projects={projects} />;
}

function DesignStart({
  query,
  projects,
}: {
  query: URLSearchParams;
  projects: Project[];
}) {
  const [question, setQuestion] = useState(query.get("question") ?? "");
  const [error, setError] = useState("");
  const { data } = useAsync<Job[]>(
    () => api.get("/jobs?command=design&limit=50") as Promise<Job[]>,
    [],
  );

  async function start(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const job = (await api.post("/jobs", {
        command: "design",
        project: projects[0]?.name ?? "default",
        question,
      })) as Job;
      go(`/designs/${job.id}`);
    } catch (err) {
      setError(errorHint(err));
    }
  }

  return (
    <div className="grid-2">
      <form className="card" onSubmit={start}>
        <h1>Design</h1>
        <p className="lede">
          State the numbers and DevLens does the arithmetic. Sections it cannot
          derive say which input they need rather than guessing.
        </p>
        <ErrorNote message={error} />
        <label className="field" htmlFor="design-question">
          <span>Requirements</span>
          <textarea
            id="design-question"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ingest 10 million events per day, 40KB payloads, p99 under 200ms"
            required
          />
        </label>
        <button className="primary" type="submit">
          Create worksheet
        </button>
      </form>
      <section className="card" aria-labelledby="worksheets">
        <h2 id="worksheets">Worksheets</h2>
        {(data ?? []).length === 0 ? (
          <Empty>
          <strong>No worksheets yet</strong>
          A worksheet does the arithmetic from numbers you state — throughput,
          retention, fan-out. Any figure it cannot derive stays marked unknown.
        </Empty>
        ) : (
          (data ?? []).map((job) => (
            <div key={job.id}>
              <a href={`#/designs/${job.id}`}>{jobTitle(job)}</a>{" "}
              <RunStateChip value={job.status} />
            </div>
          ))
        )}
      </section>
    </div>
  );
}

function DesignWorkspace({ jobId }: { jobId: string }) {
  const { job, error } = useJob(jobId);
  const [events, setEvents] = useState("10000000");
  const [peak, setPeak] = useState("10");
  const [payload, setPayload] = useState("40");
  const { data: plan } = useAsync<Capacity>(
    () =>
      api.get(
        `/capacity?events_per_day=${encodeURIComponent(events)}&peak_multiplier=${encodeURIComponent(peak)}&payload_kb=${encodeURIComponent(payload)}`,
      ) as Promise<Capacity>,
    [events, peak, payload],
  );

  if (error) return <ErrorNote message={error} />;
  if (!job) return <p>Opening worksheet…</p>;

  const result = (job.result ?? {}) as Record<string, unknown>;
  const facts = (result["facts"] as string[] | undefined) ?? [];
  const unknowns = (result["unknowns"] as string[] | undefined) ?? [];
  const derived = facts.filter((item) => !item.includes("UNKNOWN —"));
  const missing = facts.filter((item) => item.includes("UNKNOWN —"));

  return (
    <div>
      <header className="header-run">
        <div>
          <h1>{String(job.request["question"] ?? "Design")}</h1>
          <p className="lede">{String(result["executive_summary"] ?? "")}</p>
        </div>
        <RunStateChip value={job.status} />
      </header>

      <section className="card" aria-labelledby="capacity">
        <h2 id="capacity">Capacity</h2>
        <div className="grid-3">
          <label className="field" htmlFor="cap-events">
            <span>Events per day</span>
            <input
              id="cap-events"
              inputMode="numeric"
              value={events}
              onChange={(event) => setEvents(event.target.value)}
            />
          </label>
          <label className="field" htmlFor="cap-peak">
            <span>Peak multiplier</span>
            <input
              id="cap-peak"
              inputMode="numeric"
              value={peak}
              onChange={(event) => setPeak(event.target.value)}
            />
          </label>
          <label className="field" htmlFor="cap-payload">
            <span>Payload KB</span>
            <input
              id="cap-payload"
              inputMode="numeric"
              value={payload}
              onChange={(event) => setPayload(event.target.value)}
            />
          </label>
        </div>
        {plan && (
          <p>
            Average {plan.average_per_sec}/s · peak {plan.peak_per_sec}/s · daily
            ingress {plan.daily_ingress_gb} GB
          </p>
        )}
        <p className="meta">{plan?.method}</p>
      </section>

      <div className="grid-2">
        <section className="card" aria-labelledby="derived">
          <h2 id="derived">Derived from what you stated</h2>
          {derived.length === 0 ? (
            <Empty>Nothing could be derived from the stated requirements.</Empty>
          ) : (
            <ul>
              {derived.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </section>
        <section className="card" aria-labelledby="needs-input">
          <h2 id="needs-input">Needs input</h2>
          {missing.length === 0 ? (
            <Empty>Every section is derived.</Empty>
          ) : (
            <ul>
              {missing.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
          {unknowns.length > 0 && (
            <p className="meta">{unknowns.length} sections await input.</p>
          )}
        </section>
      </div>
    </div>
  );
}
