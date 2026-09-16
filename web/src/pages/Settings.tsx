import { Capabilities, Integration, api } from "../api";
import { useAsync } from "../hooks";
import { Empty, ErrorNote } from "../ui";

const SECTIONS = [
  "general",
  "security",
  "models",
  "agent",
  "usage",
  "audit",
  "members",
  "teams",
] as const;

export function SettingsHub({ section }: { section: string }) {
  const current = SECTIONS.includes(section as never) ? section : "general";
  const { data, error } = useAsync<Record<string, unknown>>(
    () => api.get(`/settings/${current}`) as Promise<Record<string, unknown>>,
    [current],
  );

  return (
    <div>
      <h1>Settings</h1>
      <p className="lede">
        Everything here is configured by environment variable or{" "}
        <code>devlens.toml</code>. DevLens has no UI that writes configuration,
        and it never shows a credential.
      </p>
      <nav className="tabs" aria-label="Settings sections">
        {SECTIONS.map((item) => (
          <a
            key={item}
            className={`tab ${current === item ? "active" : ""}`}
            href={`#/settings/${item}`}
            aria-current={current === item ? "page" : undefined}
          >
            {item}
          </a>
        ))}
      </nav>
      <ErrorNote message={error} />
      <div className="card">{data ? renderSection(current, data) : <p>Loading…</p>}</div>
    </div>
  );
}

function renderSection(section: string, data: Record<string, unknown>) {
  if (section === "audit") {
    const events = (data["events"] as Record<string, unknown>[] | undefined) ?? [];
    return events.length === 0 ? (
      <Empty>
        No audit events. Set <code>DEVLENS_AUDIT_LOG</code> to persist them.
      </Empty>
    ) : (
      <table className="table">
        <caption className="visually-hidden">Recent audit events</caption>
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Event</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event, index) => (
            <tr key={String(event["id"] ?? index)}>
              <td className="meta">
                {new Date(Number(event["ts"] ?? 0) * 1000).toLocaleString()}
              </td>
              <td>{String(event["event"] ?? "—")}</td>
              <td className="meta">
                {Object.entries(event)
                  .filter(([key]) => !["id", "ts", "event"].includes(key))
                  .map(([key, value]) => `${key}=${String(value)}`)
                  .join(" ")
                  .slice(0, 200)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  if (section === "agent") {
    const capabilities = data["capabilities"] as Capabilities | undefined;
    return (
      <div>
        <dl className="provenance">
          <dt>Execution model</dt>
          <dd>{String(data["execution_model"])}</dd>
          <dt>Approval policy</dt>
          <dd>{String(data["approval_policy"])}</dd>
        </dl>
        <h3>Capabilities</h3>
        <ul>
          {(capabilities?.enabled ?? []).map((name) => (
            <li key={name}>
              <strong>{name}</strong> — enabled
            </li>
          ))}
          {(capabilities?.denied ?? []).map((name) => (
            <li key={name}>
              <strong>{name}</strong> — {capabilities?.reasons[name]}{" "}
              <code>{capabilities?.enable_with[name]}</code>
            </li>
          ))}
        </ul>
        <h3>Resilience</h3>
        <pre className="excerpt">{JSON.stringify(data["resilience"], null, 2)}</pre>
      </div>
    );
  }
  return (
    <dl className="provenance">
      {Object.entries(data)
        .filter(([key]) => key !== "section")
        .map(([key, value]) => (
          <div key={key} className="pair">
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd>
              {typeof value === "object" && value !== null ? (
                <pre className="excerpt">{JSON.stringify(value, null, 2)}</pre>
              ) : (
                String(value)
              )}
            </dd>
          </div>
        ))}
    </dl>
  );
}

export function IntegrationsPage() {
  const { data, error } = useAsync<Integration[]>(
    () => api.get("/integrations") as Promise<Integration[]>,
    [],
  );
  const items = data ?? [];

  return (
    <div>
      <h1>Integrations</h1>
      <p className="lede">
        Read access first. DevLens never requests write scope when read is
        enough, and it has no connect flow: every integration is configured by
        environment variable so credentials stay out of the browser.
      </p>
      <ErrorNote message={error} />
      <div className="cards">
        {items.map((item) => {
          const healthy = item.status === "healthy";
          return (
            <article className="card integration" key={item.name}>
              <div className="row">
                <h3>{item.name}</h3>
                <span className={`chip run run-${healthy ? "completed" : "cancelled"}`}>
                  <span className="glyph" aria-hidden="true">
                    ◆
                  </span>
                  {healthy ? "configured" : "not configured"}
                </span>
              </div>
              <p className="meta">Scope: {item.scope}</p>
              <p className="meta">{item.configured_by}</p>
            </article>
          );
        })}
      </div>
      <p className="meta" style={{ marginTop: 16 }}>
        "Configured" means the credentials are present. DevLens reports each
        provider's real availability on the run that uses it, not here.
      </p>
    </div>
  );
}
