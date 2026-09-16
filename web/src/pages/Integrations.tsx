import { useEffect, useState } from "react";
import { Integration, api, errorHint } from "../api";

const CATALOG = [
  "jira",
  "github",
  "bitbucket",
  "gitlab",
  "loki",
  "prometheus",
  "grafana",
  "sentry",
  "postgres",
  "mysql",
  "redis",
  "rabbitmq",
  "kubernetes",
  "aws",
];

const WIZARD = ["Connect", "Authenticate", "Select scope", "Test connection", "Permissions", "Finish"];

export function IntegrationsPage() {
  const [items, setItems] = useState<Integration[]>([]);
  const [error, setError] = useState("");
  const [wizard, setWizard] = useState<string | null>(null);
  const [step, setStep] = useState(0);

  useEffect(() => {
    api.get("/integrations").then((data) => setItems(data as Integration[])).catch((err) => {
      setError(errorHint(err instanceof Error ? err.message : "Integrations unavailable"));
    });
  }, []);

  const byName = Object.fromEntries(items.map((item) => [item.name, item.status]));

  return (
    <div>
      <h1>Integrations</h1>
      <p className="lede">Read access first. Write access is never requested when read is enough.</p>
      {error ? <p className="error">{error}</p> : null}
      <div className="cards">
        {CATALOG.map((name) => {
          const status = byName[name] || "not_configured";
          return (
            <article className="card integration" key={name}>
              <div className="row">
                <h3>{name}</h3>
                <span className={`chip ${status === "healthy" ? "status-completed" : "status-cancelled"}`}>
                  <span className="dot" />
                  {status === "healthy" ? "Connected" : "Not configured"}
                </span>
              </div>
              <p className="meta">
                {status === "healthy"
                  ? "Read scope only. Last check from this process."
                  : name === "loki" || name === "prometheus"
                    ? "Live queries stay UNAVAILABLE. Investigations continue without this source."
                    : "Connect from environment or devlens.toml. Tokens stay on the server."}
              </p>
              <button className="ghost" type="button" onClick={() => { setWizard(name); setStep(0); }}>
                {status === "healthy" ? "Configure" : "Connect"}
              </button>
            </article>
          );
        })}
      </div>
      {wizard && (
        <div className="palette" onClick={() => setWizard(null)} role="presentation">
          <div className="palette-box" onClick={(event) => event.stopPropagation()}>
            <h2>{wizard}</h2>
            <div className="stages">
              {WIZARD.map((item, index) => (
                <span key={item} className={`stage ${index === step ? "now" : index < step ? "done" : ""}`}>{item}</span>
              ))}
            </div>
            <p>Read access only. Write access is not requested. Credentials stay in environment variables.</p>
            <div className="row">
              <button className="ghost" type="button" onClick={() => setWizard(null)}>Close</button>
              {step < WIZARD.length - 1 && (
                <button className="primary" type="button" onClick={() => setStep((value) => value + 1)}>Continue</button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
