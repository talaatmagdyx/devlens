import { defineConfig } from "@playwright/test";

/**
 * Real-browser end-to-end tests.
 *
 * These run the compiled UI against a real DevLens process — no mocked fetch,
 * no jsdom. The backend starts with no credentials and every capability off,
 * which is the state the tool ships in, so what these tests exercise is what a
 * new operator sees on first run.
 *
 * Set PW_BASE_URL to point the suite at a DevLens that is already running —
 * the container image, for instance — in which case Playwright must not start
 * one of its own. That is the same suite proving the shipped artifact rather
 * than a working copy, so it has to be the same file, not a forked config.
 */
const externalBaseURL = process.env.PW_BASE_URL;
const baseURL = externalBaseURL ?? "http://127.0.0.1:8111";
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "off",
    screenshot: "off",
  },
  webServer: externalBaseURL
    ? undefined
    : {
        command:
          "cd .. && DEVLENS_UI_DIR=web/dist DEVLENS_JOBS_DB=/tmp/e2e-jobs.sqlite " +
          "DEVLENS_ANALYZE_ROOT=/tmp/e2e-workspace " +
          ".venv/bin/python -m uvicorn devlens.app.api:app --host 127.0.0.1 --port 8111",
        url: "http://127.0.0.1:8111/health",
        reuseExistingServer: false,
        timeout: 60_000,
      },
  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
        launchOptions: { executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" },
      },
    },
  ],
});
