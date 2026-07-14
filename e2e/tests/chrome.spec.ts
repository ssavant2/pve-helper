import { test, expect } from "@playwright/test";

// Interaction net: exercises the app-shell controllers (sidebar, nav tree,
// toggles, global search, taskbar, column picker). These need no seeded data, so
// they run against the empty e2e-web app yet cover ~10 init* controllers in
// app.js — if the JS split drops or mis-wires one, the matching test fails.

test.beforeEach(async ({ page }) => {
  await page.goto("/vms/overview/", { waitUntil: "load" });
  // wait for app.js to finish wiring handlers
  await expect
    .poll(() =>
      page.evaluate(
        () => typeof (window as unknown as { pveHelperRefreshRecentTasks?: unknown }).pveHelperRefreshRecentTasks,
      ),
    )
    .toBe("function");
});

test("sidebar collapse toggle flips aria-expanded", async ({ page }) => {
  const toggle = page.locator("[data-sidebar-toggle]");
  const before = await toggle.getAttribute("aria-expanded");
  await toggle.click();
  await expect.poll(() => toggle.getAttribute("aria-expanded")).not.toBe(before);
});

test("nav tree module expand/collapse toggles aria-expanded", async ({ page }) => {
  const treeToggle = page.locator("[data-tree-toggle]").first();
  const before = await treeToggle.getAttribute("aria-expanded");
  await treeToggle.click();
  await expect.poll(() => treeToggle.getAttribute("aria-expanded")).not.toBe(before);
});

test("VM/CT ID toggle flips the guest-name style", async ({ page }) => {
  const before = await page.evaluate(() => document.documentElement.dataset.guestNameStyle);
  await page.locator("[data-guest-id-toggle]").click();
  const after = await page.evaluate(() => document.documentElement.dataset.guestNameStyle);
  expect(after).not.toBe(before);
  expect(["id-name", "name-only"]).toContain(after);
});

test("IPv4/IPv6 toggle flips the ip-version style", async ({ page }) => {
  const before = await page.evaluate(() => document.documentElement.dataset.ipVersionStyle);
  await page.locator("[data-ip-version-toggle]").click();
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.ipVersionStyle)).not.toBe(before);
});

test("taskbar collapse toggle flips aria-expanded", async ({ page }) => {
  const toggle = page.locator("[data-taskbar-toggle]");
  const before = await toggle.getAttribute("aria-expanded");
  await toggle.click();
  await expect.poll(() => toggle.getAttribute("aria-expanded")).not.toBe(before);
});

test("retryable tag failure retries in place and refreshes its task row", async ({ page }) => {
  const row = page.locator('[data-task-retryable="true"]');
  await expect(row).toBeVisible();
  const taskId = await row.getAttribute("data-task-id");
  expect(taskId).toBeTruthy();
  let retried = false;
  await page.route("**/tasks/retry/", async (route) => {
    retried = true;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, queued_task_id: "e2e-retry-task" }),
    });
  });
  await page.route("**/tasks/recent/**", async (route) => {
    const tasks = retried
      ? [
          {
            id: taskId,
            kind: "guest",
            action: "tag.bulk_operation",
            name: "Tag operation",
            target: "old-tag",
            target_guest: null,
            status: "Queued",
            status_class: "queued",
            details: "Retry requested",
            initiator: "e2e",
            queued_for: "-",
            started_at: "-",
            started_at_ms: 0,
            finished_at: "-",
            finished_at_ms: 0,
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
  await page.evaluate(() => {
    (window as Window & { retrySoftNavigationMarker?: string }).retrySoftNavigationMarker = "preserved";
  });
  await expect(row.locator('[data-column="status"]')).toContainText("Failed — right-click for options");
  await row.click({ button: "right" });
  const retry = page.locator('#context-menu [data-task-action="retry-task"]');
  await expect(retry).toBeEnabled();
  await retry.click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog.getByRole("heading", { name: "Retry tag operation" })).toBeVisible();
  await dialog.getByRole("button", { name: "Retry", exact: true }).click();

  await expect.poll(() => retried).toBe(true);
  await page.evaluate(() =>
    (window as unknown as { pveHelperRefreshRecentTasks?: () => void }).pveHelperRefreshRecentTasks?.(),
  );
  await expect(page.locator(`[data-task-row-key="${taskId}"] [data-column="status"]`)).toContainText("Queued");
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as Window & { retrySoftNavigationMarker?: string }).retrySoftNavigationMarker,
      ),
    )
    .toBe("preserved");
});

test("global search shows a clear button after typing and clears it", async ({ page }) => {
  const input = page.locator("[data-global-search-input]");
  await input.click();
  await input.fill("ubuntu");
  const clear = page.locator("[data-global-search-clear]");
  await expect(clear).toBeVisible();
  await clear.click();
  await expect(input).toHaveValue("");
});

test("overview column picker toggles a column's visibility", async ({ page }) => {
  // Open the "Show Columns" picker.
  const picker = page.locator("[data-column-picker] > summary").first();
  await picker.click();
  const vmidToggle = page.locator('[data-column-toggle="vmid"]');
  const vmidHeader = page.locator('th[data-column="vmid"]');
  // vmid starts hidden (unchecked by default); enabling it shows the header.
  const wasVisible = await vmidHeader.isVisible();
  await vmidToggle.click();
  await expect.poll(() => vmidHeader.isVisible()).not.toBe(wasVisible);
});
