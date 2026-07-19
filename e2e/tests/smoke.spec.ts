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
  { name: "Cluster connections", path: "/clusters/" },
  { name: "Add cluster", path: "/clusters/add/" },
  { name: "Cluster connection detail", path: "/clusters/e2e/connection/" },
  { name: "Tags", path: "/clusters/e2e/tags/" },
  { name: "Datastores", path: "/datastores/" },
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

test("header displays the configured application version", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".brand-version")).toHaveText("DEV");
});

test("cluster connection UI separates immutable identity from write-only credentials", async ({ page }) => {
  await page.goto("/clusters/");
  await expect(page.getByRole("heading", { name: "Cluster connections" })).toBeVisible();
  await expect(page.getByRole("link", { name: "E2E cluster", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Add cluster", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Add Proxmox cluster" })).toBeVisible();
  await expect(page.getByLabel("Cluster key")).toBeVisible();
  await expect(page.getByText("cannot be renamed later")).toBeVisible();
  await expect(page.locator('input[name="token_secret"]')).toHaveCount(0);

  await page.goto("/clusters/e2e/connection/");
  await expect(page.getByText("Permanent key")).toBeVisible();
  const secret = page.locator('input[name="token_secret"]');
  await expect(secret).toHaveValue("");
  await expect(secret).toHaveAttribute("autocomplete", "new-password");
});

test("tag links use soft navigation", async ({ page }) => {
  await page.goto("/clusters/e2e/tags/");
  await page.evaluate(() => {
    (window as Window & { tagSoftNavigationMarker?: string }).tagSoftNavigationMarker = "preserved";
  });
  await page.getByRole("link", { name: "prod", exact: true }).first().click();
  await expect(page).toHaveURL(/\/clusters\/e2e\/tags\/detail\/\?tag=prod/);
  await expect
    .poll(() => page.evaluate(() => (window as Window & { tagSoftNavigationMarker?: string }).tagSoftNavigationMarker))
    .toBe("preserved");
});

test("tag administration uses aligned controls and the guest editor separates new tags", async ({ page }) => {
  await page.goto("/clusters/e2e/tags/");
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

  await page.goto("/vms/e2e/vm/100/edit/?section=tags");
  await expect(page.getByLabel("Existing tags")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Create new tag" })).toBeVisible();
  await expect(page.getByText("The new cluster tag will be assigned to this object.")).toBeVisible();
});

test("partial tag inventory is labelled without hiding known membership", async ({ page }) => {
  await page.goto("/clusters/e2e/tags/", { waitUntil: "load" });

  const warning = page.locator(".tag-warning", { hasText: "Membership inventory is partial" });
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("pve2 unavailable");
  await expect(page.getByRole("link", { name: "prod", exact: true }).first()).toBeVisible();
  await expect(page.locator('#tags-table tbody tr[data-filter-text="prod Ad-hoc"] td:last-child')).toHaveText("1");
});

test("tag inventory refresh queues work and soft-refreshes after completion", async ({ page }) => {
  let queued = false;
  await page.route("**/clusters/e2e/tags/refresh/", async (route) => {
    queued = true;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, task_id: "guest:999", queued_task_id: "worker-999" }),
    });
  });
  await page.route("**/tasks/recent/**", async (route) => {
    const tasks = queued
      ? [
          {
            id: "guest:999",
            kind: "guest",
            action: "tag.inventory.refresh",
            name: "Refresh tag inventory",
            target: "cluster",
            target_guest: null,
            status: "Completed",
            status_class: "completed",
            details: "Registry and membership; 1/1 endpoints",
            initiator: "e2e",
            queued_for: "-",
            started_at: "2026-07-14 12:00:00",
            started_at_ms: Date.now(),
            finished_at: "2026-07-14 12:00:01",
            finished_at_ms: Date.now() + 60_000,
            server: "pve1",
            cancelable: false,
            retryable: false,
            offer_force_stop: false,
          },
        ]
      : [];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        tasks,
        page: 0,
        limit: 5,
        total: tasks.length,
        has_previous: false,
        has_next: false,
        start_index: tasks.length ? 1 : 0,
        end_index: tasks.length,
      }),
    });
  });
  await page.goto("/clusters/e2e/tags/", { waitUntil: "load" });
  await page.evaluate(() => {
    (window as Window & { tagRefreshMarker?: string }).tagRefreshMarker = "preserved";
  });

  await page.getByRole("button", { name: "Refresh tag inventory" }).click();

  await expect.poll(() => page.evaluate(() => (window as Window & { tagRefreshMarker?: string }).tagRefreshMarker)).toBe("preserved");
  await expect(page.getByRole("button", { name: "Refresh tag inventory" })).toBeEnabled();
  await expect(page.locator('[data-task-row-key="guest:999"]')).toContainText("Completed");
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
    "/static/css/app/clusters.css",
    "/static/css/app.css",
  ]);

  for (const href of hrefs) {
    const response = await page.request.get(href);
    expect(response.ok(), `${href} loads`).toBe(true);
  }
});
