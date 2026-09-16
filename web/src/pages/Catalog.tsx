import { FormEvent, useState } from "react";
import {
  KnowledgeDoc,
  Project,
  SearchResult,
  ServiceItem,
  api,
  errorHint,
  go,
} from "../api";
import { useAsync } from "../hooks";
import { Dialog, Empty, ErrorNote, Logo } from "../ui";

export function LandingPage({ onEnter }: { onEnter: () => void }) {
  return (
    <div className="landing">
      <header className="row" style={{ justifyContent: "space-between" }}>
        <div className="brand">
          <Logo />
          <span>DevLens</span>
        </div>
        <button className="primary" type="button" onClick={onEnter}>
          Open DevLens
        </button>
      </header>
      <h1>Understand why your software behaves the way it does.</h1>
      <p className="lede">
        Evidence-first analysis from Jira ticket to production symptom. DevLens
        reports what it verified, what it could not reach, and what stays
        unknown.
      </p>
      <section className="cards" style={{ marginTop: 24 }}>
        <article className="card">
          <h3>Evidence, not assertions</h3>
          <p>
            Every claim carries provenance. A provider that cannot be reached is
            recorded as unknown, never as support.
          </p>
        </article>
        <article className="card">
          <h3>Deterministic by default</h3>
          <p>
            No model is used unless you name one. Ticket, analyze and review
            produce full output with no model at all.
          </p>
        </article>
        <article className="card">
          <h3>Nothing written without approval</h3>
          <p>
            Remote writes require an operator to approve one specific payload.
            Merge and deploy stay human-only.
          </p>
        </article>
      </section>
    </div>
  );
}

export function OnboardingPage() {
  const { data, error, reload } = useAsync<{
    steps: { id: string; done: boolean }[];
    complete: boolean;
  }>(() => api.get("/onboarding") as never, []);
  const [markError, setMarkError] = useState("");

  async function mark(step: string) {
    try {
      await api.post("/onboarding", { step });
      reload();
    } catch (err) {
      setMarkError(errorHint(err));
    }
  }

  if (!data) return <p>Preparing workspace…</p>;
  return (
    <div className="card">
      <h1>Set up DevLens</h1>
      <ErrorNote message={markError || error} />
      <ol className="checklist">
        {data.steps.map((step) => (
          <li key={step.id} className="row">
            <span>
              <span aria-hidden="true">{step.done ? "✓" : "○"}</span>{" "}
              <span className="visually-hidden">
                {step.done ? "done: " : "not done: "}
              </span>
              {step.id.replaceAll("_", " ")}
            </span>
            {!step.done && (
              <button className="ghost" type="button" onClick={() => mark(step.id)}>
                Mark done
              </button>
            )}
          </li>
        ))}
      </ol>
      <a className="primary" href="#/investigations">
        Start an investigation
      </a>
    </div>
  );
}

export function ServicesPage({ id }: { id: string }) {
  const { data, error } = useAsync<ServiceItem[]>(
    () => api.get("/services") as Promise<ServiceItem[]>,
    [],
  );
  const items = data ?? [];
  const selected = items.find((item) => item.id === id);

  if (error) return <ErrorNote message={error} />;

  if (id && selected) {
    return (
      <div>
        <h1>{selected.name}</h1>
        <p className="meta">
          {selected.origin} · {selected.provider ?? "no provider"} ·{" "}
          {selected.repository ?? "no repository"}
        </p>
        <div className="grid-2">
          <section className="card">
            <h2>Declared</h2>
            <dl className="provenance">
              <dt>Language</dt>
              <dd>{selected.language ?? "—"}</dd>
              <dt>Framework</dt>
              <dd>{selected.framework ?? "—"}</dd>
              <dt>Owners</dt>
              <dd>{selected.owners.join(", ") || "—"}</dd>
              <dt>Databases</dt>
              <dd>{selected.databases.join(", ") || "—"}</dd>
              <dt>Queues</dt>
              <dd>{selected.queues.join(", ") || "—"}</dd>
            </dl>
          </section>
          <section className="card">
            <h2>Dependencies</h2>
            {selected.dependencies.length === 0 ? (
              <Empty>
                None declared. Add a <code>[services]</code> entry to devlens.toml
                to make cross-repository investigation possible.
              </Empty>
            ) : (
              <ul className="files">
                {selected.dependencies.map((name) => (
                  <li key={name}>{name}</li>
                ))}
              </ul>
            )}
            <p className="meta">Open findings: {selected.findings}</p>
          </section>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h1>Services</h1>
      <p className="lede">
        Declared in <code>devlens.toml</code> and derived from the repository
        allowlist. DevLens does not discover services from a running system.
      </p>
      {items.length === 0 ? (
        <Empty>
          <strong>No services yet</strong>
          The catalog is built from the repository allowlist and declared
          services — never from automatic discovery, which DevLens does not do.
          Add a repository in <a href="#/settings">Configuration</a>.
        </Empty>
      ) : (
        <div className="cards">
          {items.map((item) => (
            <a className="quick" key={item.id} href={`#/services/${item.id}`}>
              <strong>{item.name}</strong>
              <p>
                {item.repository ?? item.origin} · findings {item.findings}
              </p>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

export function KnowledgePage({ id }: { id: string }) {
  const { data, error, reload } = useAsync<KnowledgeDoc[]>(
    () => api.get("/knowledge") as Promise<KnowledgeDoc[]>,
    [],
  );
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [category, setCategory] = useState("incidents");
  const [saveError, setSaveError] = useState("");

  const items = data ?? [];
  const selected = items.find((item) => item.id === id);

  async function create(event: FormEvent) {
    event.preventDefault();
    setSaveError("");
    try {
      const doc = (await api.post("/knowledge", {
        category,
        title,
        body,
      })) as KnowledgeDoc;
      setTitle("");
      setBody("");
      reload();
      go(`/knowledge/${doc.id}`);
    } catch (err) {
      setSaveError(errorHint(err));
    }
  }

  if (selected) {
    return (
      <article className="card">
        <div className="meta">{selected.category}</div>
        <h1>{selected.title}</h1>
        <p style={{ whiteSpace: "pre-wrap" }}>{selected.body}</p>
        <a className="ghost" href="#/knowledge">
          Back
        </a>
      </article>
    );
  }

  const categories = [
    "adrs",
    "incidents",
    "postmortems",
    "runbooks",
    "architecture",
    "standards",
  ];
  const filtered = items
    .filter(
      (item) =>
        !query ||
        `${item.title} ${item.body} ${item.category}`
          .toLowerCase()
          .includes(query.toLowerCase()),
    )
    .filter((item) => !filter || item.category === filter);

  return (
    <div className="grid-2">
      <div>
        <h1>Knowledge</h1>
        <ErrorNote message={error} />
        <label className="field" htmlFor="knowledge-search">
          <span className="visually-hidden">Search knowledge</span>
          <input
            id="knowledge-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search ADRs, incidents, runbooks…"
          />
        </label>
        <div className="row" style={{ margin: "12px 0" }}>
          {["", ...categories].map((item) => (
            <button
              key={item || "all"}
              className={`ghost ${filter === item ? "primary" : ""}`}
              type="button"
              aria-pressed={filter === item}
              onClick={() => setFilter(item)}
            >
              {item || "all"}
            </button>
          ))}
        </div>
        {filtered.length === 0 ? (
          <Empty>
          <strong>No documents yet</strong>
          Knowledge is what DevLens has read and can cite — a design document, a
          runbook, a ticket thread. Nothing is summarised from memory.
        </Empty>
        ) : (
          <ul className="files">
            {filtered.map((item) => (
              <li key={item.id}>
                <a href={`#/knowledge/${item.id}`}>
                  {item.category} · {item.title}
                </a>
              </li>
            ))}
          </ul>
        )}
      </div>
      <form className="card" onSubmit={create}>
        <h2>Add document</h2>
        <ErrorNote message={saveError} />
        <label className="field" htmlFor="doc-category">
          <span>Category</span>
          <select
            id="doc-category"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
          >
            {categories.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="field" htmlFor="doc-title">
          <span>Title</span>
          <input
            id="doc-title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            required
          />
        </label>
        <label className="field" htmlFor="doc-body">
          <span>Body</span>
          <textarea
            id="doc-body"
            value={body}
            onChange={(event) => setBody(event.target.value)}
            required
          />
        </label>
        <button className="primary" type="submit">
          Save
        </button>
      </form>
    </div>
  );
}

export function RepositoriesPage({
  id,
  projects,
}: {
  id: string;
  projects: Project[];
}) {
  const repos = projects.flatMap((item) =>
    item.repositories.map((name) => ({
      id: name.replace("/", "-"),
      name,
      project: item.name,
      provider: item.git_provider,
    })),
  );
  const selected = repos.find((item) => item.id === id || item.name === id);

  if (selected) {
    return (
      <div>
        <h1>{selected.name}</h1>
        <p className="meta">
          {selected.provider} · project {selected.project} · allowlisted for read
          access
        </p>
        <section className="card">
          <h2>What DevLens can do with this repository</h2>
          <ul>
            <li>
              Analyse a ticket against it — <a href="#/investigations">Investigate</a>
            </li>
            <li>
              Review a pull request — <a href="#/reviews">Reviews</a>
            </li>
            <li>
              Plan an implementation — <a href="#/implementations">Implement</a>
            </li>
          </ul>
          <p className="meta">
            DevLens does not maintain a file or symbol index. Use Ask with a
            local checkout path to search source directly.
          </p>
        </section>
      </div>
    );
  }

  return (
    <div>
      <h1>Repositories</h1>
      <p className="lede">Every repository on a project allowlist.</p>
      {repos.length === 0 ? (
        <Empty>
          <strong>No repositories configured</strong>
          <code>DEVLENS_REPOSITORIES</code> is the allowlist: a repository that
          is not on it cannot be read, cloned or searched.
        </Empty>
      ) : (
        <table className="table">
          <caption className="visually-hidden">Allowlisted repositories</caption>
          <thead>
            <tr>
              <th scope="col">Repository</th>
              <th scope="col">Provider</th>
              <th scope="col">Project</th>
            </tr>
          </thead>
          <tbody>
            {repos.map((item) => (
              <tr key={item.name}>
                <td>
                  <a href={`#/repositories/${item.id}`}>{item.name}</a>
                </td>
                <td>{item.provider}</td>
                <td>{item.project}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function SearchOverlay({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [error, setError] = useState("");

  async function run(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      setResult(
        (await api.get(`/search?q=${encodeURIComponent(query)}`)) as SearchResult,
      );
    } catch (err) {
      setError(errorHint(err));
    }
  }

  return (
    <Dialog title="Search" onClose={onClose} labelledBy="search-title">
      <form onSubmit={run}>
        <label className="field" htmlFor="search-input">
          <span className="visually-hidden">Search runs, knowledge and services</span>
          <input
            id="search-input"
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search runs, findings, knowledge, services…"
          />
        </label>
        <button className="primary" type="submit">
          Search
        </button>
      </form>
      <ErrorNote message={error} />
      {result &&
        Object.entries(result.groups).map(([group, items]) =>
          items.length ? (
            <div key={group}>
              <div className="rail-group">
                {group} ({items.length})
              </div>
              <ul className="palette-list">
                {items.map((item) => (
                  <li key={item.href + item.title}>
                    <button
                      type="button"
                      onClick={() => {
                        go(item.href);
                        onClose();
                      }}
                    >
                      {item.title}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null,
        )}
    </Dialog>
  );
}
