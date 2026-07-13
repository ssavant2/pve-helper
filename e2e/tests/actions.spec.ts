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
  // The rendered chips are authoritative if scan/registry metadata is stale.
  await taggedRow.evaluate((row) => {
    delete (row as HTMLElement).dataset.guestTags;
  });
  await page.locator("#vm-overview-tag-options").evaluate((script) => {
    script.textContent = "[]";
  });
  await taggedRow.click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Tags" }).hover();
  await expect(page.locator('#context-menu [data-vm-action="remove-tags"]')).toBeEnabled();
  await expect(page.locator('#context-menu [data-vm-action="add-tags"]')).toBeEnabled();
  await expect(page.locator('#context-menu [data-vm-action="edit-tags"]')).toHaveCount(0);

  await page.locator('#context-menu [data-vm-action="remove-tags"]').click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog.getByRole("heading", { name: "Remove Tags" })).toBeVisible();
  await expect(dialog.locator('select[name="tags_value"]')).toHaveValue("prod");
  await expect(dialog.getByText(/Replace all/i)).toHaveCount(0);
  await dialog.locator("[data-vm-dialog-close]").click();

  const untaggedRow = page.locator('[data-vm-overview-row][data-guest-vmid="101"]');
  await untaggedRow.click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Tags" }).hover();
  await expect(page.locator('#context-menu [data-vm-action="remove-tags"]')).toBeDisabled();
  await page.locator('#context-menu [data-vm-action="add-tags"]').click();
  await expect(dialog.getByRole("heading", { name: "Add Tags" })).toBeVisible();
  await expect(dialog.locator('select[name="tags_value"]')).toHaveValue("prod");
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
