import { FormEvent, useState } from "react";
import {
  EvidenceItem,
  Job,
  Project,
  ProviderResult,
  api,
  errorHint,
  go,
  jobTitle,
} from "../api";
import { useJob } from "../hooks";
import {
  Empty,
  ErrorNote,
  Live,
  ProviderChip,
  RunStateChip,
  StatusChip,
  runAnnouncement,
} from "../ui";

export function AskPage({
  query,
  projects,
}: {
  query: URLSearchParams;
  projects: Project[];
}) {
  const run = query.get("run") ?? "";
  if (run) return <AskResult jobId={run} />;
  return <AskForm query={query} projects={projects} />;
}

function AskForm({
  query,
  projects,
}: {
  query: URLSearchParams;
  projects: Project[];
}) {
  const [question, setQuestion] = useState(query.get("question") ?? "");
  const [path, setPath] = useState(query.get("path") ?? "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function start(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const job = (await api.post("/jobs", {
        command: "ask",
        project: projects[0]?.name ?? "default",
        question,
        path: path || null,
      })) as Job;
      go(`/ask?run=${job.id}`);
    } catch (err) {
      setError(errorHint(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card ask-flow" onSubmit={start}>
      <h1>Ask</h1>
      <p className="lede">
        Question, then sources, then an answer. Every claim traces to an evidence
        card. This is not a chat thread.
      </p>
      <ErrorNote message={error} />
      <label className="field" htmlFor="ask-question">
        <span>Question</span>
        <textarea
          id="ask-question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          required
          placeholder="Why does the reply delivery path use Redis here?"
        />
      </label>
      <label className="field" htmlFor="ask-path">
        <span>Local repository path (optional)</span>
        <input
          id="ask-path"
          value={path}
          onChange={(event) => setPath(event.target.value)}
        />
      </label>
      <button className="primary" disabled={busy} type="submit">
        {busy ? "Inspecting…" : "Ask DevLens"}
      </button>
    </form>
  );
}

function AskResult({ jobId }: { jobId: string }) {
  const { job, error, events } = useJob(jobId);
  if (error) return <ErrorNote message={error} />;
  if (!job) return <p>Inspecting sources…</p>;

  const result = (job.result ?? {}) as Record<string, unknown>;
  const sources = (result["evidence_items"] as EvidenceItem[] | undefined) ?? [];
  const providers = (result["provider_results"] as ProviderResult[] | undefined) ?? [];
  const facts = (result["facts"] as string[] | undefined) ?? [];
  const running = job.status === "queued" || job.status === "running";

  return (
    <div className="ask-flow">
      <div>
        <div className="ask-step">Question</div>
        <h1>{String(job.request["question"] ?? jobTitle(job))}</h1>
        <RunStateChip value={job.status} />
      </div>

      <section className="card" aria-labelledby="sources">
        <div className="ask-step" id="sources">
          Sources
        </div>
        <p className="meta">{sources.length} inspected</p>
        {sources.map((item) => (
          <div className="evidence" key={item.id}>
            <div className="row">
              <strong>{item.source}</strong>
              <StatusChip value={item.status} />
            </div>
            <p>{item.observation}</p>
          </div>
        ))}
        {providers.length > 0 && (
          <div className="row" style={{ marginTop: 12 }}>
            {providers.map((item) => (
              <span key={item.provider}>
                <ProviderChip value={item.status} /> {item.provider}
              </span>
            ))}
          </div>
        )}
      </section>

      <section className="card" aria-labelledby="reasoning">
        <div className="ask-step" id="reasoning">
          What was inspected
        </div>
        <p className="lede">A record of the work, not a hidden chain of thought.</p>
        {facts.length === 0 ? (
          <Empty>{running ? "Inspecting sources…" : "Nothing was inspected."}</Empty>
        ) : (
          <ul>
            {facts.slice(0, 8).map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        )}
      </section>

      <section className="card" aria-labelledby="answer">
        <div className="ask-step" id="answer">
          Answer
        </div>
        <p>
          {String(result["executive_summary"] ?? job.progress ?? "Collecting evidence.")}
        </p>
        <ul>
          {((result["unknowns"] as string[] | undefined) ?? []).map((item) => (
            <li key={item} className="meta">
              {item}
            </li>
          ))}
        </ul>
      </section>
      <Live
        message={runAnnouncement(
          job.status,
          events[events.length - 1]?.message,
          job.progress,
        )}
      />
    </div>
  );
}
