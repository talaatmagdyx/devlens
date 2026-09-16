import { FormEvent, useMemo, useState } from "react";
import {
  Capabilities,
  Finding,
  Job,
  Project,
  Proposal,
  Severity,
  Truncation,
  api,
  errorHint,
  go,
  jobTitle,
} from "../api";
import { DiffViewer } from "../DiffViewer";
import { useAsync, useJob } from "../hooks";
import {
  ConfidenceChip,
  Empty,
  ErrorNote,
  RunStateChip,
  SeverityChip,
  StatusChip,
} from "../ui";
import { ProposalList } from "./Operations";

const SEVERITIES: Severity[] = ["P0", "P1", "P2", "P3"];

export function ReviewsPage({
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
  if (id) return <ReviewWorkspace jobId={id} capabilities={capabilities} />;
  return <ReviewStart projects={projects} query={query} capabilities={capabilities} />;
}

function ReviewStart({
  projects,
  query,
  capabilities,
}: {
  projects: Project[];
  query: URLSearchParams;
  capabilities: Capabilities | null;
}) {
  const { data } = useAsync<Job[]>(
    () => api.get("/jobs?command=review&limit=50") as Promise<Job[]>,
    [],
  );
  const [project, setProject] = useState("");
  const [repository, setRepository] = useState(query.get("repository") ?? "");
  const [pr, setPr] = useState(query.get("pr") ?? "");
  const [ticket, setTicket] = useState(query.get("ticket") ?? "");
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
        command: "review",
        project: chosen,
        repository: chosenRepo || null,
        pr: Number(pr),
        ticket_key: ticket || null,
        run_tests: runTests,
      })) as Job;
      go(`/reviews/${job.id}`);
    } catch (err) {
      setError(errorHint(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid-2">
      <form className="card" onSubmit={start}>
        <h1>Reviews</h1>
        <p className="lede">
          Deterministic heuristics anchored to the lines this pull request
          changed. Severity and evidence status stay separate.
        </p>
        <ErrorNote message={error} />
        <label className="field" htmlFor="rev-project">
          <span>Project</span>
          <select
            id="rev-project"
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
        <label className="field" htmlFor="rev-repo">
          <span>Repository</span>
          <input
            id="rev-repo"
            list="repo-options"
            value={chosenRepo}
            onChange={(event) => setRepository(event.target.value)}
            placeholder="org/repo"
            required
          />
          <datalist id="repo-options">
            {available.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </label>
        <label className="field" htmlFor="rev-pr">
          <span>Pull request number</span>
          <input
            id="rev-pr"
            inputMode="numeric"
            value={pr}
            onChange={(event) => setPr(event.target.value)}
            required
          />
        </label>
        <label className="field" htmlFor="rev-ticket">
          <span>Ticket (optional)</span>
          <input
            id="rev-ticket"
            value={ticket}
            onChange={(event) => setTicket(event.target.value)}
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={runTests && canRunTests}
            disabled={!canRunTests}
            onChange={(event) => setRunTests(event.target.checked)}
          />
          <span>
            Run the repository's own tests
            {!canRunTests && (
              <em className="meta">
                {" "}
                — {capabilities?.reasons["repository_code_execution"]}
              </em>
            )}
          </span>
        </label>
        <button className="primary" disabled={busy} type="submit">
          {busy ? "Queuing…" : "Review pull request"}
        </button>
      </form>

      <section className="card" aria-labelledby="recent-reviews">
        <h2 id="recent-reviews">Recent reviews</h2>
        {(data ?? []).length === 0 ? (
          <Empty>
          <strong>No reviews yet</strong>
          Point DevLens at a pull request. Findings are anchored to the lines the
          diff changed, from deterministic heuristics rather than a model's
          impression.
        </Empty>
        ) : (
          <ul className="files">
            {(data ?? []).map((job) => (
              <li key={job.id}>
                <a href={`#/reviews/${job.id}`}>{jobTitle(job)}</a>{" "}
                <RunStateChip value={job.status} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function ReviewWorkspace({
  jobId,
  capabilities,
}: {
  jobId: string;
  capabilities: Capabilities | null;
}) {
  const { job, error } = useJob(jobId);
  const [file, setFile] = useState("");

  const result = (job?.result ?? {}) as Record<string, unknown>;
  const pull = result["pull_request"] as
    | { files?: { path: string }[]; title?: string; number?: number; from_fork?: boolean }
    | undefined;
  const findings = (result["findings"] as Finding[] | undefined) ?? [];
  const truncations = (result["truncations"] as Truncation[] | undefined) ?? [];
  const proposals = (result["proposals"] as Proposal[] | undefined) ?? [];
  const files = (pull?.files?.map((item) => item.path) ??
    (result["files_affected"] as string[] | undefined) ??
    []) as string[];

  const counts = useMemo(() => {
    const next: Record<Severity, number> = { P0: 0, P1: 0, P2: 0, P3: 0 };
    for (const item of findings) next[item.severity] += 1;
    return next;
  }, [findings]);

  if (error) return <ErrorNote message={error} />;
  if (!job) return <p>Opening review…</p>;

  const visible = findings.filter((item) => !file || item.file === file);

  return (
    <div>
      <header className="header-run">
        <div>
          <h1>{pull ? `PR #${pull.number} · ${pull.title}` : jobTitle(job)}</h1>
          <div className="row">
            <RunStateChip value={job.status} />
            {SEVERITIES.map((level) => (
              <span key={level} className={`chip sev sev-${level}`}>
                <span className="glyph" aria-hidden="true">
                  ■
                </span>
                {level} {counts[level]}
              </span>
            ))}
          </div>
          {pull?.from_fork && (
            <p className="meta">
              This pull request comes from a fork; its head commit is not under
              the base repository's control.
            </p>
          )}
        </div>
        {job.result && (
          <a className="ghost" href={`/jobs/${job.id}/export?format=md`}>
            Export
          </a>
        )}
      </header>

      {truncations.length > 0 && (
        <p className="meta">
          Not inspected:{" "}
          {truncations
            .map(
              (item) =>
                `${item.source} (${item.fetched}${item.total ? ` of ${item.total}` : ""})`,
            )
            .join("; ")}
        </p>
      )}

      <div className="workspace-3 no-right">
        <aside className="pane" aria-label="Changed files">
          <h3>Files</h3>
          <ul className="files">
            <li>
              <button
                className={file === "" ? "active" : ""}
                type="button"
                onClick={() => setFile("")}
              >
                All files
              </button>
            </li>
            {files.map((path) => (
              <li key={path}>
                <button
                  className={file === path ? "active" : ""}
                  type="button"
                  onClick={() => setFile(path)}
                >
                  {path}
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <section className="pane main" aria-label="Diff and findings">
          <h3>Diff</h3>
          {typeof result["diff"] === "string" && result["diff"] ? (
            <DiffViewer value={result["diff"] as string} selected={file || undefined} />
          ) : (
            <Empty>
              {job.status === "running" ? "Reading the pull request…" : "No diff."}
            </Empty>
          )}

          <h3>Findings</h3>
          {visible.length === 0 ? (
            <Empty>
              No findings for this selection. A clean heuristic review is not
              proof the change is correct.
            </Empty>
          ) : (
            visible.map((item) => (
              <article className="finding" key={item.id}>
                <div className="row">
                  <SeverityChip value={item.severity} />
                  <StatusChip value={item.status} />
                  <ConfidenceChip value={item.confidence} />
                  <span className="meta">
                    {item.file}:{item.line ?? "-"} · {item.category}
                  </span>
                </div>
                <strong>{item.title}</strong>
                {item.production_scenario && (
                  <p>Production scenario: {item.production_scenario}</p>
                )}
                {item.impact && <p>Impact: {item.impact}</p>}
                {item.recommended_fix && <p>Fix: {item.recommended_fix}</p>}
                {item.verification && <p className="meta">Verify: {item.verification}</p>}
              </article>
            ))
          )}

          <ProposalList proposals={proposals} capabilities={capabilities} />
        </section>
      </div>
    </div>
  );
}
