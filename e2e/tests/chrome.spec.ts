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

test("retryable tag failure exposes an in-place retry action", async ({ page }) => {
  const row = page.locator('[data-task-retryable="true"]');
  await expect(row).toBeVisible();
  await expect(row.locator('[data-column="status"]')).toContainText("Failed — right-click for options");
  await row.click({ button: "right" });
  const retry = page.locator('#context-menu [data-task-action="retry-task"]');
  await expect(retry).toBeEnabled();
  await retry.click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog.getByRole("heading", { name: "Retry tag operation" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
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
