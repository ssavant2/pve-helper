import { test, expect } from "@playwright/test";

// Data-dependent flows against the two seeded guests (100 running, 101 stopped).
// These cover the highest split-risk JS: row selection, the right-click context
// menu, and — most important — the shared openConfirmDialog that all destructive
// actions now route through. PVE is disabled, so we only assert the dialog opens
// (the submit would no-op); opening it is the behaviour the split must preserve.

test.beforeEach(async ({ page }) => {
  await page.goto("/vms/overview/", { waitUntil: "load" });
  await expect(page.locator('[data-vm-select]').first()).toBeVisible();
});

test("selecting a row updates the selection status", async ({ page }) => {
  const status = page.locator("[data-vm-selection-status]");
  await expect(status).toHaveText(/0 selected/);
  await page.locator("[data-vm-select]").first().check();
  await expect(status).toHaveText(/1 selected/);
});

test("right-click opens the context menu", async ({ page }) => {
  const menu = page.locator("#context-menu");
  await expect(menu).toBeHidden();
  await page.locator("[data-vm-overview-row]").first().click({ button: "right" });
  await expect(menu).toBeVisible();
});

test("Tags menu offers existing tags for add and assigned tags for remove", async ({ page }) => {
  const taggedRow = page.locator('[data-vm-overview-row][data-guest-vmid="100"]');
  await page.route("**/vms/vm/100/tag-options/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ available_tags: ["prod", "qa"], assigned_tags: ["prod"] }),
    });
  });
  // The rendered chips are authoritative if scan/registry metadata is stale.
  await taggedRow.evaluate((row) => {
    delete (row as HTMLElement).dataset.guestTags;
  });
  await page.locator("#vm-overview-tag-options").evaluate((script) => {
    script.textContent = '["prod","qa"]';
  });
  await taggedRow.click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Tags" }).hover();
  await expect(page.locator('#context-menu [data-vm-action="remove-tags"]')).toBeEnabled();
  await expect(page.locator('#context-menu [data-vm-action="add-tags"]')).toBeEnabled();
  await expect(page.locator('#context-menu [data-vm-action="edit-tags"]')).toHaveCount(0);

  await page.locator('#context-menu [data-vm-action="add-tags"]').click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog.getByRole("heading", { name: "Add Tags" })).toBeVisible();
  await expect(dialog.locator('select[name="tags_value"] option')).toHaveText(["qa"]);
  await dialog.locator("[data-vm-dialog-close]").click();

  await taggedRow.click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Tags" }).hover();

  await page.locator('#context-menu [data-vm-action="remove-tags"]').click();
  await expect(dialog.getByRole("heading", { name: "Remove Tags" })).toBeVisible();
  await expect(dialog.locator('select[name="tags_value"]')).toHaveValue("prod");
  await expect(dialog.getByText(/Replace all/i)).toHaveCount(0);
  await dialog.locator("[data-vm-dialog-close]").click();

  const untaggedRow = page.locator('[data-vm-overview-row][data-guest-vmid="101"]');
  await untaggedRow.click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Tags" }).hover();
  await expect(page.locator('#context-menu [data-vm-action="remove-tags"]')).toBeEnabled();
  await page.locator('#context-menu [data-vm-action="add-tags"]').click();
  await expect(dialog.getByRole("heading", { name: "Add Tags" })).toBeVisible();
  await expect(dialog.locator('select[name="tags_value"]')).toHaveValue("prod");
});

test("Tags menu receives registry and membership data in the VM workspace", async ({ page }) => {
  await page.goto("/vms/", { waitUntil: "load" });
  await page.route("**/vms/vm/100/tag-options/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ available_tags: ["prod", "qa"], assigned_tags: ["prod"] }),
    });
  });
  const tagOptions = page.locator("#vm-overview-tag-options");
  await expect.poll(() => tagOptions.evaluate((script) => JSON.parse(script.textContent || "[]"))).toContain("prod");
  await tagOptions.evaluate((script) => {
    script.textContent = '["prod","qa"]';
  });
  const taggedGuest = page.locator('.guest-list-item[data-guest-vmid="100"]');
  await taggedGuest.click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Tags" }).hover();
  await expect(page.locator('#context-menu [data-vm-action="remove-tags"]')).toBeEnabled();
  await page.locator('#context-menu [data-vm-action="add-tags"]').click();
  await expect(page.locator('[data-vm-action-dialog] select[name="tags_value"] option')).toHaveText(["qa"]);
});

test("successful destroy navigates away from the deleted guest summary", async ({ page }) => {
  await page.goto("/vms/vm/101/summary/", { waitUntil: "load" });
  await page.route("**/vms/bulk-action/", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, errors: [] }) });
      return;
    }
    await route.continue();
  });

  const stoppedGuest = page.locator('.guest-list-item[data-guest-vmid="101"]');
  await stoppedGuest.click({ button: "right" });
  await page.locator('#context-menu [data-vm-action="destroy"]').click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await dialog.locator('input[name="destroy_confirm_vmid"]').fill("101");
  await dialog.getByRole("button", { name: "Remove", exact: true }).click();

  await expect(page).toHaveURL(/\/vms\/$/);
  await expect(page.locator("[data-guest-pane]")).toBeVisible();
});

test("tag detail can remove the tag from one assigned object", async ({ page }) => {
  let submitted = "";
  let detailLoads = 0;
  let refreshWasCacheBusted = false;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (request.method() === "GET" && url.pathname === "/tags/detail/") {
      detailLoads += 1;
      refreshWasCacheBusted ||= url.searchParams.has("_tag_refresh");
    }
  });
  await page.route("**/vms/bulk-action/", async (route) => {
    if (route.request().method() === "POST") {
      submitted = route.request().postData() || "";
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, errors: [] }) });
      return;
    }
    await route.continue();
  });
  await page.goto("/tags/detail/?tag=prod", { waitUntil: "load" });

  await page.getByRole("button", { name: "Remove prod from e2e-vm-running" }).click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog.getByRole("heading", { name: "Remove tag" })).toBeVisible();
  await dialog.getByRole("button", { name: "Remove tag", exact: true }).click();

  await expect.poll(() => submitted).toContain('name="tags_mode"');
  expect(submitted).toMatch(/name="tags_mode"[\s\S]*remove/);
  expect(submitted).toMatch(/name="tags_value"[\s\S]*prod/);
  expect(submitted).toMatch(/name="guest"[\s\S]*vm:100@pve1/);
  await expect.poll(() => detailLoads).toBeGreaterThanOrEqual(2);
  expect(refreshWasCacheBusted).toBe(true);
});

test("Power Off on a running guest opens the shared confirm dialog", async ({ page }) => {
  // Power Off (stop) lives under the "Power" hover submenu; reveal it first.
  const runningRow = page.locator('[data-vm-overview-row][data-guest-status="running"]').first();
  await runningRow.click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Power" }).hover();
  await page.locator('#context-menu [data-vm-action="stop"]').click();
  // openConfirmDialog renders a danger confirm inside the shared dialog element.
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("[data-confirm-yes]")).toBeVisible();
  // Cancelling closes it without firing anything.
  await dialog.locator("[data-confirm-no]").click();
  await expect(dialog.locator("[data-confirm-yes]")).toBeHidden();
});

test("Clone... opens the clone form dialog", async ({ page }) => {
  await page.locator("[data-vm-overview-row]").first().click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Template" }).hover();
  await page.locator('#context-menu [data-vm-action="clone"]').click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/New VMID/i)).toBeVisible();
});
