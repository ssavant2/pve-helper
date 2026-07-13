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
  { name: "Tags", path: "/tags/" },
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

test("tag links use soft navigation", async ({ page }) => {
  await page.goto("/tags/");
  await page.evaluate(() => {
    (window as Window & { tagSoftNavigationMarker?: string }).tagSoftNavigationMarker = "preserved";
  });
  await page.getByRole("link", { name: "prod", exact: true }).first().click();
  await expect(page).toHaveURL(/\/tags\/detail\/\?tag=prod/);
  await expect
    .poll(() => page.evaluate(() => (window as Window & { tagSoftNavigationMarker?: string }).tagSoftNavigationMarker))
    .toBe("preserved");
});

test("tag administration uses aligned controls and the guest editor separates new tags", async ({ page }) => {
  await page.goto("/tags/");
  const createWidth = await page.locator('.tag-create-form input[name="tag"]').evaluate((element) => element.getBoundingClientRect().width);
  const filterWidth = await page.locator('input[placeholder="Filter tags"]').evaluate((element) => element.getBoundingClientRect().width);
  expect(Math.abs(createWidth - filterWidth)).toBeLessThan(1);
  const filter = page.locator('input[placeholder="Filter tags"]');
  await filter.fill("prod");
  await expect(page.locator('#tags-table tbody tr[data-filter-text="prod Ad-hoc"]')).toBeVisible();
  await filter.fill("missing-tag");
  await expect(page.locator('#tags-table tbody tr[data-filter-text="prod Ad-hoc"]')).toBeHidden();
  const overflowY = await page.locator(".tag-inventory-scroll").evaluate((element) => getComputedStyle(element).overflowY);
  expect(overflowY).toBe("auto");
  const firstRow = page.locator("#tags-table tbody tr").first();
  expect(await firstRow.evaluate((element) => element.getBoundingClientRect().height)).toBeLessThanOrEqual(34);
  await expect(firstRow.locator("td").nth(0)).toHaveCSS("border-right-width", "1px");
  await expect(firstRow.locator("td").nth(1)).toHaveCSS("border-right-width", "1px");
  await expect(firstRow.locator("td").nth(2)).toHaveCSS("border-right-width", "0px");
  await filter.fill("");
  await page.locator('#tags-table th[data-column="objects"]').click();
  const objectCounts = await page.locator("#tags-table tbody tr td:last-child").allTextContents();
  expect(objectCounts.map(Number)).toEqual([...objectCounts.map(Number)].sort((left, right) => left - right));

  await page.goto("/vms/vm/100/edit/?section=tags");
  await expect(page.getByLabel("Existing tags")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Create new tag" })).toBeVisible();
  await expect(page.getByText("The new cluster tag will be assigned to this object.")).toBeVisible();
});

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
    "/static/css/app/audit.css",
    "/static/css/app/console.css",
    "/static/css/app/taskbar.css",
    "/static/css/app/context-menu.css",
    "/static/css/app/action-dialog.css",
    "/static/css/app/shared.css",
    "/static/css/app/guest-workspace.css",
    "/static/css/app/hardware-editor.css",
    "/static/css/app/register-import.css",
    "/static/css/app/hardware-devices.css",
    "/static/css/app/guest-tabs.css",
    "/static/css/app/vm-overview.css",
    "/static/css/app/hardware-forms.css",
    "/static/css/app/tags.css",
    "/static/css/app.css",
  ]);

  for (const href of hrefs) {
    const response = await page.request.get(href);
    expect(response.ok(), `${href} loads`).toBe(true);
  }
});
