import { FormEvent, useState } from "react";
import {
  Capabilities,
  Job,
  Project,
  Proposal,
  api,
  errorHint,
  go,
  jobTitle,
} from "../api";
import { useAsync, useJob } from "../hooks";
import { Empty, ErrorNote, RunStateChip } from "../ui";
import { ProposalList } from "./Operations";

const STAGES = ["Requirement", "Context", "Plan", "Sandbox", "Review", "Approval"];

export function ImplementationsPage({
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
  if (id) return <ImplementWorkspace jobId={id} capabilities={capabilities} />;
  return <ImplementStart projects={projects} query={query} capabilities={capabilities} />;
}

function ImplementStart({
  projects,
  query,
  capabilities,
}: {
  projects: Project[];
  query: URLSearchParams;
  capabilities: Capabilities | null;
}) {
  const { data } = useAsync<Job[]>(
    () => api.get("/jobs?command=implement&limit=50") as Promise<Job[]>,
    [],
  );
  const [ticket, setTicket] = useState(query.get("ticket") ?? "");
  const [repository, setRepository] = useState(query.get("repository") ?? "");
  const [project, setProject] = useState("");
  const [runTests, setRunTests] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const chosen = project || projects[0]?.name || "default";
  const available = projects.find((item) => item.name === chosen)?.repositories ?? [];
  const chosenRepo = repository || available[0] || "";
  const canRunTests = capabilities?.enabled.includes("repository_code_execution") ?? false;

  async function start(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const job = (await api.post("/jobs", {
        command: "implement",
        project: chosen,
        ticket_key: ticket,
        repository: chosenRepo,
        run_tests: runTests,
      })) as Job;
      go(`/implementations/${job.id}`);
    } catch (err) {
      setError(errorHint(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid-2">
      <form className="card" onSubmit={start}>
        <h1>Implement</h1>
        <p className="lede">
          DevLens plans the change surface. It does not generate application
          code, and it writes nothing remote without an approved proposal.
        </p>
        <ErrorNote message={error} />
        <label className="field" htmlFor="imp-ticket">
          <span>Ticket</span>
          <input
            id="imp-ticket"
            value={ticket}
            onChange={(event) => setTicket(event.target.value)}
            placeholder="DEV-1427"
            required
          />
        </label>
        <label className="field" htmlFor="imp-repo">
          <span>Repository</span>
          <input
            id="imp-repo"
            value={chosenRepo}
            onChange={(event) => setRepository(event.target.value)}
            placeholder="org/repo"
            required
          />
        </label>
        <label className="field" htmlFor="imp-project">
          <span>Project</span>
          <select
            id="imp-project"
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
        <label className="check">
          <input
            type="checkbox"
            checked={runTests && canRunTests}
            disabled={!canRunTests}
            onChange={(event) => setRunTests(event.target.checked)}
          />
          <span>
            Establish a baseline by running the repository's tests
            {!canRunTests && (
              <em className="meta">
                {" "}
                — {capabilities?.reasons["repository_code_execution"]}
              </em>
            )}
          </span>
        </label>
        <button className="primary" disabled={busy} type="submit">
          {busy ? "Planning…" : "Create plan"}
        </button>
      </form>

      <section className="card" aria-labelledby="plans">
        <h2 id="plans">Plans</h2>
        {(data ?? []).length === 0 ? (
          <Empty>
          <strong>No plans yet</strong>
          Give DevLens a ticket and it will produce a plan and proposals. It does
          not write application code, and it never merges or deploys.
        </Empty>
        ) : (
          (data ?? []).map((job) => (
            <div key={job.id}>
              <a href={`#/implementations/${job.id}`}>{jobTitle(job)}</a>{" "}
              <RunStateChip value={job.status} />
            </div>
          ))
        )}
      </section>
    </div>
  );
}

function ImplementWorkspace({
  jobId,
  capabilities,
}: {
  jobId: string;
  capabilities: Capabilities | null;
}) {
  const { job, error } = useJob(jobId);
  if (error) return <ErrorNote message={error} />;
  if (!job) return <p>Preparing the plan…</p>;

  const result = (job.result ?? {}) as Record<string, unknown>;
  const plan = (result["implementation_plan"] as string[] | undefined) ?? [];
  const files = (result["files_affected"] as string[] | undefined) ?? [];
  const risks = (result["risks"] as string[] | undefined) ?? [];
  const unknowns = (result["unknowns"] as string[] | undefined) ?? [];
  const observations = (result["observations"] as string[] | undefined) ?? [];
  const proposals = (result["proposals"] as Proposal[] | undefined) ?? [];
  const current =
    job.status === "completed" ? "Plan" : job.status === "running" ? "Context" : "Requirement";

  return (
    <div>
      <header className="header-run">
        <div>
          <h1>{jobTitle(job)}</h1>
          <p className="lede">
            {String(result["executive_summary"] ?? "Building a change-surface plan.")}
          </p>
        </div>
        <RunStateChip value={job.status} />
      </header>

      <ol className="stages" aria-label="Implementation stages">
        {STAGES.map((item) => {
          const state =
            item === current
              ? "now"
              : STAGES.indexOf(item) < STAGES.indexOf(current)
                ? "done"
                : "";
          return (
            <li key={item} className={`stage ${state}`} aria-current={state === "now" ? "step" : undefined}>
              {item}
            </li>
          );
        })}
      </ol>

      <div className="grid-2">
        <section className="card" aria-labelledby="plan">
          <h2 id="plan">Implementation plan</h2>
          <p className="meta">
            {files.length} files in the change surface. DevLens generates no code.
          </p>
          {plan.length === 0 ? (
            <Empty>
              {job.status === "running" ? "Reading the ticket and repository…" : "No plan produced."}
            </Empty>
          ) : (
            <ol>
              {plan.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ol>
          )}
          {files.length > 0 && (
            <>
              <h3>Files</h3>
              <ul className="files">
                {files.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </>
          )}
        </section>

        <section className="card" aria-labelledby="verification">
          <h2 id="verification">Verification</h2>
          <ul>
            {((result["verification_plan"] as string[] | undefined) ?? []).map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          {risks.length > 0 && (
            <>
              <h3>Risks</h3>
              <ul>
                {risks.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </>
          )}
          {unknowns.length > 0 && (
            <>
              <h3>Unknowns</h3>
              <ul>
                {unknowns.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </>
          )}
          {observations.length > 0 && (
            <>
              <h3>Sandbox</h3>
              <ul>
                {observations.map((item) => (
                  <li key={item} className="meta">
                    {item}
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>

      <ProposalList proposals={proposals} capabilities={capabilities} />
    </div>
  );
}
