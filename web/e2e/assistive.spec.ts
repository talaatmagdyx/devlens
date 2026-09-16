import { expect, test } from "@playwright/test";

/**
 * What a screen reader is handed.
 *
 * axe-core finds violations of specific rules. It cannot tell you that a
 * control's accessible name is "button", that a status change is never
 * announced, or that focus lands nowhere after a dialog closes — the things
 * that make an interface unusable without sight even when every rule passes.
 *
 * These read the browser's own accessibility tree (the same tree AT consumes)
 * and assert the semantics, not the markup. What they still cannot do is
 * replace a person running NVDA or VoiceOver, and the report says so.
 */

const SURFACES = ["/#/overview", "/#/ask", "/#/investigations", "/#/runs", "/#/approvals", "/#/settings"];

type Node = { role?: string; name?: string; children?: Node[]; [key: string]: unknown };

function flatten(node: Node | null, out: Node[] = []): Node[] {
  if (!node) return out;
  out.push(node);
  for (const child of node.children ?? []) flatten(child, out);
  return out;
}

test("every control exposes a name to the accessibility tree", async ({ page }) => {
  const unnamed: string[] = [];
  for (const surface of SURFACES) {
    await page.goto(surface);
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    const tree = (await page.accessibility.snapshot({ interestingOnly: false })) as Node | null;
    for (const node of flatten(tree)) {
      const role = String(node.role ?? "");
      if (!["button", "link", "textbox", "combobox", "checkbox", "switch", "tab"].includes(role)) continue;
      if (!String(node.name ?? "").trim()) unnamed.push(`${surface}: an unnamed ${role}`);
    }
  }
  expect(unnamed).toEqual([]);
});

test("a control's name says what it does, not what it is", async ({ page }) => {
  // "Click here", "button", "link": names that name the widget instead of the
  // action are exactly what a screen-reader user hears in a list of controls.
  const useless = /^(click here|here|button|link|more|read more|\.\.\.|…|submit|ok)$/i;
  const offenders: string[] = [];
  for (const surface of SURFACES) {
    await page.goto(surface);
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    const tree = (await page.accessibility.snapshot({ interestingOnly: false })) as Node | null;
    for (const node of flatten(tree)) {
      const role = String(node.role ?? "");
      const name = String(node.name ?? "").trim();
      if (["button", "link"].includes(role) && useless.test(name)) {
        offenders.push(`${surface}: ${role} named "${name}"`);
      }
    }
  }
  expect(offenders).toEqual([]);
});

test("each surface has one first-level heading and skips no level", async ({ page }) => {
  const problems: string[] = [];
  for (const surface of SURFACES) {
    await page.goto(surface);
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    const levels = await page.evaluate(() =>
      [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")]
        .filter((el) => (el as HTMLElement).offsetParent !== null)
        .map((el) => Number(el.tagName[1])),
    );
    const firsts = levels.filter((level) => level === 1).length;
    if (firsts !== 1) problems.push(`${surface}: ${firsts} <h1> elements`);
    levels.forEach((level, index) => {
      if (index > 0 && level - levels[index - 1] > 1) {
        problems.push(`${surface}: jumps from h${levels[index - 1]} to h${level}`);
      }
    });
  }
  expect(problems).toEqual([]);
});

test("the landmarks a screen reader navigates by are present and named", async ({ page }) => {
  await page.goto("/#/overview");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await expect(page.getByRole("main")).toBeVisible();
  await expect(page.getByRole("banner")).toBeVisible();
  // One main per document: two is ambiguous to anything navigating by landmark.
  expect(await page.getByRole("main").count()).toBe(1);
  expect(await page.locator("html[lang]").count()).toBe(1);
});

test("the document title names the surface", async ({ page }) => {
  // Announced on navigation, and the only cue in a tab list or window switcher.
  const titles: string[] = [];
  for (const surface of SURFACES) {
    await page.goto(surface);
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    await page.waitForFunction(() => document.title !== "");
    titles.push(await page.title());
  }
  expect(new Set(titles).size, `titles seen: ${titles.join(", ")}`).toBe(SURFACES.length);
  for (const title of titles) expect(title).toContain("DevLens");
});

test("a run announces its progress through a live region", async ({ page }) => {
  await page.goto("/#/ask");
  const live = page.locator("[aria-live]");
  await expect(live.first()).toBeAttached();

  const announcements: string[] = [];
  await page.exposeFunction("recordAnnouncement", (text: string) => {
    announcements.push(text);
  });
  // Observe the document, not today's regions: submitting navigates within the
  // single page, so the region that carries the run's progress does not exist
  // yet at the moment this is wired up.
  await page.evaluate(() => {
    const seen = new Set<string>();
    new MutationObserver(() => {
      for (const region of document.querySelectorAll("[aria-live]")) {
        const text = (region as HTMLElement).innerText.trim();
        if (text && !seen.has(text)) {
          seen.add(text);
          // @ts-expect-error bound above
          window.recordAnnouncement(text);
        }
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  });

  await page.getByLabel("Question").fill("what does idempotency mean for a retry?");
  await page.getByRole("button", { name: "Ask DevLens" }).click();
  await expect
    .poll(() => announcements.length, { timeout: 20_000, message: "nothing was ever announced" })
    .toBeGreaterThan(0);
});

test("an error is announced, not only coloured", async ({ page }) => {
  await page.goto("/#/investigations");
  // A path outside the configured root is refused by the server; the operator
  // has to hear about it, so it must land in an alert region.
  await page.getByLabel("Question or symptom").fill("checkout times out under load");
  await page.getByLabel(/Local repository path/).fill("../../etc/passwd");
  await page.getByRole("button", { name: "Start investigation" }).click();
  await expect(page.locator('[role="alert"], [aria-live="assertive"]').first()).toBeVisible({
    timeout: 20_000,
  });
});

test("the dialog takes focus, traps it, and gives it back", async ({ page }) => {
  await page.goto("/#/overview");
  const before = await page.evaluate(() => document.activeElement?.tagName ?? "");
  await page.keyboard.press("Control+k");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();

  // Focus is inside the dialog, and tabbing repeatedly never escapes it.
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  for (let i = 0; i < 12; i += 1) await page.keyboard.press("Tab");
  expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  const after = await page.evaluate(() => document.activeElement?.tagName ?? "");
  // Focus must not be left on <body>: from there the next Tab starts over at
  // the top of the page, losing the reader's place entirely.
  expect(`${before} -> ${after}`).not.toContain("-> BODY");
});

test("nothing is reachable by mouse alone", async ({ page }) => {
  await page.goto("/#/overview");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  const unreachable = await page.evaluate(() => {
    const problems: string[] = [];
    for (const element of document.querySelectorAll<HTMLElement>("[onclick], .clickable, [role=button]")) {
      const tag = element.tagName.toLowerCase();
      const focusable =
        tag === "button" ||
        tag === "a" ||
        tag === "input" ||
        element.tabIndex >= 0;
      if (!focusable) problems.push(`${tag}.${element.className} is clickable but not focusable`);
    }
    return problems;
  });
  expect(unreachable).toEqual([]);
});

test("the visible focus indicator is not suppressed", async ({ page }) => {
  await page.goto("/#/overview");
  // After the application has hydrated: pressing Tab at an empty document
  // focuses nothing and would pass this test for the wrong reason.
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await page.keyboard.press("Tab");
  const visible = await page.evaluate(() => {
    const element = document.activeElement as HTMLElement | null;
    if (!element || element === document.body) return false;
    const style = getComputedStyle(element);
    const outline = style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0;
    const ring = style.boxShadow !== "none";
    return outline || ring;
  });
  expect(visible, "the first tab stop shows no focus ring").toBe(true);
});
