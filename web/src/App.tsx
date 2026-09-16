import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  AuthStatus,
  Capabilities,
  Notice,
  Project,
  Route,
  api,
  errorHint,
  go,
  route,
} from "./api";
import { useAnnouncer } from "./hooks";
import { AskPage } from "./pages/Ask";
import {
  KnowledgePage,
  LandingPage,
  OnboardingPage,
  RepositoriesPage,
  SearchOverlay,
  ServicesPage,
} from "./pages/Catalog";
import { DesignsPage } from "./pages/Design";
import { ImplementationsPage } from "./pages/Implement";
import { InvestigationsPage } from "./pages/Investigation";
import { ApprovalsPage, ObservePage, RunsPage } from "./pages/Operations";
import { OverviewPage } from "./pages/Overview";
import { ReviewsPage } from "./pages/Review";
import { IntegrationsPage, SettingsHub } from "./pages/Settings";
import { Dialog, ErrorNote, Live, Logo } from "./ui";

/* Five destinations, not thirteen.
 *
 * The old rail listed every surface at the same level, so "Design" — one form —
 * sat beside "Runs", where the operator actually lives, and four of the five
 * groups held a single page each. These are the five things an operator comes
 * here to do; everything else is a tab inside one of them, and every old URL
 * still resolves.
 */
const NAV = [
  { id: "overview", label: "Overview", href: "/overview" },
  { id: "analyze", label: "Analyze", href: "/ask" },
  { id: "operations", label: "Operations", href: "/runs" },
  { id: "catalog", label: "Catalog", href: "/services" },
  { id: "configuration", label: "Configuration", href: "/settings" },
] as const;

/** Which section a page belongs to, and the tabs that section shows. */
const SECTIONS: Record<string, { section: string; title: string; tabs: [string, string][] }> = {};

const SECTION_TABS: Record<string, [string, string][]> = {
  analyze: [
    ["ask", "Ask"],
    ["investigations", "Investigate"],
    ["reviews", "Review"],
    ["implementations", "Implement"],
    ["designs", "Design"],
    ["observe", "Observe"],
  ],
  operations: [
    ["runs", "Runs"],
    ["approvals", "Approvals"],
  ],
  catalog: [
    ["services", "Services"],
    ["repositories", "Repositories"],
    ["knowledge", "Knowledge"],
  ],
  configuration: [
    ["settings", "Settings"],
    ["integrations", "Integrations"],
  ],
};

for (const [section, tabs] of Object.entries(SECTION_TABS)) {
  for (const [page, title] of tabs) {
    SECTIONS[page] = { section, title, tabs };
  }
}

const PALETTE: [string, string][] = [
  ["Ask DevLens", "/ask"],
  ["Investigate a symptom", "/investigations"],
  ["Review a pull request", "/reviews"],
  ["Plan an implementation", "/implementations"],
  ["Create a design worksheet", "/designs"],
  ["Scan for observability gaps", "/observe"],
  ["View runs", "/runs"],
  ["Open approvals", "/approvals"],
  ["Browse services", "/services"],
  ["Browse repositories", "/repositories"],
  ["Search knowledge", "/knowledge"],
  ["Check integrations", "/integrations"],
];

export function App() {
  const [loc, setLoc] = useState<Route>(route());
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const [palette, setPalette] = useState(false);
  const [search, setSearch] = useState(false);
  const [notes, setNotes] = useState<Notice[]>([]);
  const [showNotes, setShowNotes] = useState(false);
  // Three states, not two: an operator who has never touched the toggle gets
  // whatever their machine is set to, which is what every other tool on their
  // desktop does. Only an explicit choice is stored and stamped.
  const [theme, setTheme] = useState<"system" | "light" | "dark">(() => {
    try {
      const stored = localStorage.getItem("devlens-theme");
      if (stored === "light" || stored === "dark") return stored;
    } catch {
      /* private browsing */
    }
    return "system";
  });
  const [announcement, announce] = useAnnouncer();

  useEffect(() => {
    if (theme === "system") {
      delete document.documentElement.dataset["theme"];
    } else {
      document.documentElement.dataset["theme"] = theme;
    }
    try {
      if (theme === "system") localStorage.removeItem("devlens-theme");
      else localStorage.setItem("devlens-theme", theme);
    } catch {
      /* private browsing */
    }
  }, [theme]);

  const dark =
    theme === "dark" ||
    (theme === "system" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);

  // The title is the only thing a screen reader announces on navigation in a
  // single-page application, and the only label in a tab strip or window
  // switcher. React never touches it on its own, so a route change left every
  // surface called "DevLens".
  useEffect(() => {
    const label =
      SECTIONS[loc.page]?.title ??
      NAV.find((entry) => entry.id === loc.page)?.label;
    document.title = label ? `${label} · DevLens` : "DevLens";
  }, [loc.page]);

  useEffect(() => {
    const onHash = () => setLoc(route());
    const onKey = (event: KeyboardEvent) => {
      const meta = event.metaKey || event.ctrlKey;
      if (meta && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPalette((open) => !open);
      }
      if (meta && event.key === "/") {
        event.preventDefault();
        setSearch((open) => !open);
      }
      if (event.key === "Escape") setShowNotes(false);
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  useEffect(() => {
    api
      .get("/auth/status")
      .then((data) => setAuth(data as AuthStatus))
      .catch(() => setAuth({ required: true, authenticated: false, session_seconds: 0 }));
  }, []);

  const signedIn = Boolean(auth && (!auth.required || auth.authenticated));

  useEffect(() => {
    if (!signedIn) return;
    void api
      .get("/projects")
      .then((data) => setProjects(data as Project[]))
      .catch(() => undefined);
    void api
      .get("/capabilities")
      .then((data) => setCapabilities(data as Capabilities))
      .catch(() => undefined);
    void api
      .get("/notifications")
      .then((data) => setNotes(data as Notice[]))
      .catch(() => undefined);
  }, [signedIn]);

  const login = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setError("");
      try {
        const next = (await api.post("/auth/login", { password })) as AuthStatus;
        setAuth({ ...next, authenticated: true, session_seconds: next.session_seconds ?? 0 });
        setPassword("");
        announce("Signed in.");
      } catch (err) {
        setError(errorHint(err));
      }
    },
    [password, announce],
  );

  if (!auth) return <div className="login card">Loading DevLens…</div>;

  if (loc.page === "welcome") {
    return (
      <main className="main">
        <LandingPage onEnter={() => go("/overview")} />
      </main>
    );
  }

  if (auth.required && !auth.authenticated) {
    return (
      <form className="login card" onSubmit={login}>
        <div className="brand">
          <Logo />
          <span>DevLens</span>
        </div>
        <p className="lede">
          Operator sign-in. Tokens stay on the server and are never sent to the
          browser.
        </p>
        <ErrorNote message={error} />
        <label className="field" htmlFor="password">
          <span>Password</span>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        <button className="primary" type="submit">
          Sign in
        </button>
        <Live message={announcement} />
      </form>
    );
  }

  return (
    <div className={`app ${collapsed ? "collapsed" : ""}`}>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="top">
        <a className="brand" href="#/overview">
          <Logo />
          <span>DevLens</span>
        </a>
        <button className="top-search" type="button" onClick={() => setPalette(true)}>
          Ask, investigate, review…
          <kbd>⌘K</kbd>
        </button>
        <div className="top-actions">
          <button
            className="icon-btn"
            type="button"
            onClick={() => setSearch(true)}
            aria-label="Global search"
          >
            ⌕
          </button>
          <button
            className="icon-btn"
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-pressed={collapsed}
          >
            ☰
          </button>
          <button
            className="icon-btn"
            type="button"
            onClick={() => setShowNotes((value) => !value)}
            aria-label={`Notifications (${notes.length})`}
            aria-expanded={showNotes}
          >
            ◔
          </button>
          <button
            className="icon-btn"
            type="button"
            onClick={() => setTheme(dark ? "light" : "dark")}
            aria-label={`Switch to ${dark ? "light" : "dark"} theme`}
          >
            {dark ? "☼" : "☾"}
          </button>
        </div>
        {showNotes && (
          <div className="notes" role="region" aria-label="Notifications">
            {notes.length === 0 ? (
              <p className="empty">No notifications.</p>
            ) : (
              notes.map((item) => (
                <a
                  key={item.id}
                  href={`#${item.href}`}
                  onClick={() => setShowNotes(false)}
                >
                  <strong>{item.title}</strong>
                  <p>{item.body}</p>
                </a>
              ))
            )}
          </div>
        )}
      </header>
      <nav className="rail" aria-label="Main">
        {NAV.map((entry) => {
          const here =
            loc.page === entry.id || SECTIONS[loc.page]?.section === entry.id;
          return (
            <a
              key={entry.id}
              className={`nav-link ${here ? "active" : ""}`}
              href={`#${entry.href}`}
              aria-current={here ? "page" : undefined}
            >
              <span>{entry.label}</span>
            </a>
          );
        })}
      </nav>
      <main className="main" id="main" tabIndex={-1}>
        <div>
        {SECTIONS[loc.page] && (
          <nav className="subnav" aria-label="Section">
            {SECTIONS[loc.page]!.tabs.map(([page, label]) => (
              <a
                key={page}
                href={`#/${page}`}
                aria-current={loc.page === page ? "page" : undefined}
              >
                {label}
              </a>
            ))}
          </nav>
        )}
        {loc.page === "overview" && <OverviewPage />}
        {loc.page === "ask" && <AskPage query={loc.query} projects={projects} />}
        {loc.page === "investigations" && (
          <InvestigationsPage
            id={loc.id}
            query={loc.query}
            projects={projects}
            capabilities={capabilities}
          />
        )}
        {loc.page === "reviews" && (
          <ReviewsPage id={loc.id} query={loc.query} projects={projects} capabilities={capabilities} />
        )}
        {loc.page === "implementations" && (
          <ImplementationsPage id={loc.id} query={loc.query} projects={projects} capabilities={capabilities} />
        )}
        {loc.page === "designs" && <DesignsPage id={loc.id} query={loc.query} projects={projects} />}
        {loc.page === "observe" && <ObservePage query={loc.query} capabilities={capabilities} />}
        {loc.page === "runs" && <RunsPage id={loc.id} />}
        {loc.page === "approvals" && <ApprovalsPage capabilities={capabilities} />}
        {loc.page === "integrations" && <IntegrationsPage />}
        {loc.page === "settings" && <SettingsHub section={loc.id} />}
        {loc.page === "services" && <ServicesPage id={loc.id} />}
        {loc.page === "repositories" && <RepositoriesPage id={loc.id} projects={projects} />}
        {loc.page === "knowledge" && <KnowledgePage id={loc.id} />}
        {loc.page === "onboarding" && <OnboardingPage />}
        </div>
      </main>
      <footer className="foot">
        DevLens · evidence-first engineering analysis
      </footer>
      {palette && <CommandPalette onClose={() => setPalette(false)} />}
      {search && <SearchOverlay onClose={() => setSearch(false)} />}
      <Live message={announcement} />
    </div>
  );
}

function CommandPalette({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState("");
  const items = PALETTE.filter(([label]) =>
    label.toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <Dialog title="Commands" onClose={onClose} labelledBy="palette-title">
      <label className="field" htmlFor="palette-input">
        <span className="visually-hidden">Filter commands</span>
        <input
          id="palette-input"
          autoFocus
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Ask, investigate, review, implement…"
        />
      </label>
      <ul className="palette-list">
        {items.map(([label, href]) => (
          <li key={href}>
            <button
              type="button"
              onClick={() => {
                go(href);
                onClose();
              }}
            >
              {label}
            </button>
          </li>
        ))}
        {items.length === 0 && <li className="empty">No matching command.</li>}
      </ul>
    </Dialog>
  );
}
