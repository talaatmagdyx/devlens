import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

/**
 * First run: a real browser against a real DevLens process with no credentials.
 *
 * Everything asserted here is what an operator actually sees before configuring
 * anything. The point of running this in a browser rather than jsdom is that it
 * catches what jsdom cannot: a bundle that fails to parse, a stylesheet that
 * never loads, a request to a host that is not there, a control that is
 * invisible or unclickable rather than merely absent from the DOM.
 */

test("the application boots with no console error and no failed request", async ({ page }) => {
  const problems: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
  page.on("requestfailed", (request) =>
    problems.push(`requestfailed: ${request.url()} ${request.failure()?.errorText}`),
  );

  await page.goto("/#/overview");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  expect(problems).toEqual([]);
});

test("every request the page makes stays on this origin", async ({ page, baseURL }) => {
  // Read the origin from the configuration rather than pinning it: the same
  // suite runs against a working copy and against the shipped container image,
  // which listens on a different port.
  const origin = new URL(baseURL!).origin;
  const offOrigin: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin !== origin && url.protocol !== "data:") {
      offOrigin.push(request.url());
    }
  });
  await page.goto("/#/overview");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await page.waitForTimeout(500);
  expect(offOrigin).toEqual([]);
});

test("the UI is served by the API process, not a dev server", async ({ request }) => {
  const response = await request.get("/");
  expect(response.status()).toBe(200);
  expect(response.headers()["x-content-type-options"]).toBe("nosniff");
  expect(await response.text()).toContain("<title>");
});

test("capabilities are reported as off, with the variable that turns each on", async ({ page }) => {
  await page.goto("/#/settings");
  const body = await page.request.get("/capabilities");
  const inventory = await body.json();
  expect(inventory.enabled).toEqual([]);
  expect(inventory.denied).toContain("git_writeback");
  expect(inventory.enable_with.git_writeback).toBe("DEVLENS_ALLOW_WRITES=1");
});

test("a deterministic run completes end to end and shows its evidence", async ({ page }) => {
  await page.goto("/#/ask");
  await page.getByLabel("Question").fill("what does idempotency mean for a retry?");
  await page.getByRole("button", { name: "Ask DevLens" }).click();

  await expect(page.locator("#answer")).toBeVisible({ timeout: 20_000 });
  // No language model is configured, so the run must say what it inspected
  // rather than producing prose from nothing, and must count its evidence
  // honestly rather than asserting an answer.
  await expect(page.locator("#sources")).toBeVisible();
  await expect(page.getByText(/not a hidden chain of thought/)).toBeVisible();
  await expect(page.getByText(/0 confirmed/)).toBeVisible();
});

test("a refused path is reported to the operator, not swallowed", async ({ page }) => {
  await page.goto("/#/observe");
  await page.getByLabel("Local path").fill("/etc");
  await page.getByRole("button", { name: "Scan workspace" }).click();
  await expect(page.getByRole("alert")).toContainText(/workspace root|Not allowed/);
});

test("status is never carried by colour alone", async ({ page }) => {
  await page.goto("/#/approvals");
  await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();
  // Every chip the design system produces must carry readable text.
  const chips = page.locator(".chip");
  const count = await chips.count();
  for (let index = 0; index < count; index += 1) {
    const text = (await chips.nth(index).innerText()).trim();
    expect(text.length).toBeGreaterThan(0);
  }
});

test("the keyboard alone reaches the main surfaces", async ({ page }) => {
  await page.goto("/#/overview");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();

  await page.keyboard.press("Tab");
  const firstFocused = await page.evaluate(() => document.activeElement?.textContent ?? "");
  expect(firstFocused).toContain("Skip to content");

  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeVisible();
});

test("the command palette opens and closes from the keyboard", async ({ page }) => {
  await page.goto("/#/overview");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await page.keyboard.press("ControlOrMeta+k");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

test("no accessibility violation on the surfaces an operator lands on", async ({ page }) => {
  const axe = readFileSync(require.resolve("axe-core"), "utf8");
  const surfaces = ["/#/overview", "/#/ask", "/#/approvals", "/#/runs", "/#/settings"];
  const failures: string[] = [];

  // Both themes: a contrast failure that only exists in one of them is still a
  // contrast failure for whoever runs that one.
  for (const theme of ["dark", "light"] as const) {
    for (const surface of surfaces) {
      await page.goto(surface);
      await page.evaluate((value) => {
        document.documentElement.dataset["theme"] = value;
        try {
          localStorage.setItem("devlens-theme", value);
        } catch {
          /* private mode */
        }
      }, theme);
      await page.waitForTimeout(300);
      // Injected through the debugger rather than a <script> tag, because the
      // application's own CSP forbids inline script — which is the CSP working.
      await page.evaluate(axe);
      const result = await page.evaluate(async () => {
        // @ts-expect-error injected at runtime
        return await window.axe.run(document, {
          runOnly: { type: "tag", values: ["wcag2a", "wcag2aa"] },
        });
      });
      for (const violation of result.violations) {
        failures.push(
          `${theme} ${surface} ${violation.id} (${violation.nodes.length} nodes): ${violation.help}`,
        );
      }
    }
  }
  expect(failures).toEqual([]);
});

test("the layout holds at a phone width without a horizontal scrollbar", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 780 });
  for (const surface of ["/#/overview", "/#/ask", "/#/runs"]) {
    await page.goto(surface);
    await page.waitForTimeout(250);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, `${surface} overflows by ${overflow}px`).toBeLessThanOrEqual(1);
  }
});

test("every surface stays reachable at a phone width", async ({ page }) => {
  // The rail used to be hidden below 860px, which left the command palette —
  // a keyboard shortcut — as the only route to any other surface. Navigation is
  // now two levels: five sections in the rail, and that section's surfaces in a
  // tab bar. Both have to survive the narrow viewport, because either one going
  // missing strands the same pages.
  await page.setViewportSize({ width: 390, height: 780 });
  await page.goto("/#/overview");

  const rail = page.getByRole("navigation", { name: "Main" });
  await expect(rail).toBeVisible();
  for (const label of ["Overview", "Analyze", "Operations", "Catalog", "Configuration"]) {
    await expect(rail.getByRole("link", { name: label })).toBeAttached();
  }

  await rail.getByRole("link", { name: "Operations" }).click();
  const section = page.getByRole("navigation", { name: "Section" });
  await expect(section).toBeVisible();
  await section.getByRole("link", { name: "Approvals" }).click();
  await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();

  // And the six analysis surfaces are one tap from their section, not buried.
  await rail.getByRole("link", { name: "Analyze" }).click();
  for (const label of ["Ask", "Investigate", "Review", "Implement", "Design", "Observe"]) {
    await expect(section.getByRole("link", { name: label })).toBeAttached();
  }
});

test("the server-sent event stream delivers progress while a run is in flight", async ({
  page,
}) => {
  const frames: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/events")) frames.push(response.url());
  });
  await page.goto("/#/ask");
  await page.getByLabel("Question").fill("why do retries duplicate a charge?");
  await page.getByRole("button", { name: "Ask DevLens" }).click();
  await expect(page.locator("#answer")).toBeVisible({ timeout: 20_000 });
  expect(frames.length).toBeGreaterThan(0);
});
