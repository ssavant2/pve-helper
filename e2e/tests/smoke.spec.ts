import { test, expect } from "@playwright/test";

// Smoke net for the JS split (A2): every main page must load AND app.js must
// initialise, with no uncaught JS errors. A broken ES module stops app.js from
// running, so `window.pveHelperRefreshRecentTasks` (exposed only after the
// taskbar init runs) disappears — the strongest cheap signal that a bundle
// failed to load. This is the automated replacement for manual click-through.

const PAGES = [
  { name: "Dashboard", path: "/" },
  { name: "VMs Overview", path: "/vms/overview/" },
  { name: "VMs Inventory", path: "/vms/" },
  { name: "Audit log", path: "/audit/" },
];

for (const p of PAGES) {
  test(`${p.name} loads and app.js initialises`, async ({ page }) => {
    const jsErrors: string[] = [];
    page.on("pageerror", (err) => jsErrors.push(String(err)));

    const resp = await page.goto(p.path, { waitUntil: "load" });
    expect(resp?.status(), `${p.path} HTTP status`).toBeLessThan(400);

    // app.js is deferred; give its init a beat, then confirm it ran.
    await expect
      .poll(() => page.evaluate(() => typeof (window as unknown as { pveHelperRefreshRecentTasks?: unknown }).pveHelperRefreshRecentTasks), {
        timeout: 5_000,
      })
      .toBe("function");

    expect(jsErrors, `uncaught JS errors on ${p.path}`).toEqual([]);
  });
}

test("theme toggle button is wired (app.js event handlers attached)", async ({ page }) => {
  await page.goto("/vms/overview/", { waitUntil: "load" });
  const toggle = page.locator("[data-theme-toggle]");
  await expect(toggle).toBeVisible();
  const before = await page.evaluate(() => document.documentElement.dataset.theme);
  await toggle.click();
  await expect
    .poll(() => page.evaluate(() => document.documentElement.dataset.theme))
    .not.toBe(before);
});

test("CSS layers load in the intended cascade order", async ({ page }) => {
  await page.goto("/vms/overview/", { waitUntil: "load" });
  const hrefs = await page.locator('link[rel="stylesheet"]').evaluateAll((links) => links.map((link) => link.href));
  const paths = hrefs.map((href) => new URL(href).pathname);

  expect(paths).toEqual([
    "/static/css/app/foundation.css",
    "/static/css/app/layout.css",
    "/static/css/app/topbar.css",
    "/static/css/app/workspace.css",
    "/static/css/app/components.css",
    "/static/css/app/storage-browser.css",
    "/static/css/app/scheduling.css",
    "/static/css/app.css",
  ]);

  for (const href of hrefs) {
    const response = await page.request.get(href);
    expect(response.ok(), `${href} loads`).toBe(true);
  }
});
