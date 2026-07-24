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
  await page.route("**/vms/e2e/vm/100/tag-options/**", async (route) => {
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
  await page.route("**/vms/e2e/vm/100/tag-options/**", async (route) => {
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
  await page.goto("/vms/e2e/vm/101/summary/", { waitUntil: "load" });
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
    if (request.method() === "GET" && url.pathname === "/clusters/e2e/tags/detail/") {
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
  await page.goto("/clusters/e2e/tags/detail/?tag=prod", { waitUntil: "load" });

  await page.getByRole("button", { name: "Remove prod from e2e-vm-running" }).click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog.getByRole("heading", { name: "Remove tag" })).toBeVisible();
  await dialog.getByRole("button", { name: "Remove tag", exact: true }).click();

  await expect.poll(() => submitted).toContain('name="tags_mode"');
  expect(submitted).toMatch(/name="tags_mode"[\s\S]*remove/);
  expect(submitted).toMatch(/name="tags_value"[\s\S]*prod/);
  expect(submitted).toMatch(/name="guest"[\s\S]*gr1:e2e:vm:100@pve1/);
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

test("generic confirm forms use the shared dialog without native popups", async ({ page }) => {
  const nativeDialogs: string[] = [];
  page.on("dialog", async (dialog) => {
    nativeDialogs.push(dialog.type());
    await dialog.dismiss();
  });
  await page.goto("/clusters/e2e/tags/detail/?tag=prod", { waitUntil: "load" });

  await page.getByRole("button", { name: "Delete tag", exact: true }).click();

  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog.getByRole("heading", { name: "Delete tag" })).toBeVisible();
  await dialog.locator("[data-confirm-no]").click();
  expect(nativeDialogs).toEqual([]);
});

test("request failures render locally and never open native alerts", async ({ page }) => {
  const nativeDialogs: string[] = [];
  page.on("dialog", async (dialog) => {
    nativeDialogs.push(dialog.type());
    await dialog.dismiss();
  });
  await page.route("**/vms/e2e/vm/101/tag-options/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ available_tags: ["prod"], assigned_tags: [] }),
    });
  });
  await page.route("**/vms/bulk-action/", async (route) => {
    await route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, errors: ["Tag update was rejected."] }),
    });
  });

  const row = page.locator('[data-vm-overview-row][data-guest-vmid="101"]');
  await row.click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Tags" }).hover();
  await page.locator('#context-menu [data-vm-action="add-tags"]').click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await dialog.getByRole("button", { name: "Add", exact: true }).click();

  await expect(page.locator("[data-local-action-error]")).toHaveText("Tag update was rejected.");
  expect(nativeDialogs).toEqual([]);
});

test("Clone... opens the clone form dialog", async ({ page }) => {
  await page.locator("[data-vm-overview-row]").first().click({ button: "right" });
  await page.locator("#context-menu .context-menu-parent", { hasText: "Template" }).hover();
  await page.locator('#context-menu [data-vm-action="clone"]').click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/New VMID/i)).toBeVisible();
});

test("a confirmation chained onto another one actually opens", async ({ page }) => {
  // Regression. Every modal used to share one <dialog> element, and close()
  // fires `close` from a queued task rather than synchronously — so the second
  // dialog was already on screen when the first one's close event arrived, took
  // it for a dismissal and resolved false. The risk question after Rename and
  // the second question before a permanent delete therefore never appeared, and
  // the action was abandoned with no dialog, no request and no error.
  //
  // Driving the primitive directly is deliberate: the flows that chain dialogs
  // need risky storage data this stack does not have, which is exactly why the
  // suite missed the bug.
  await page.evaluate(async () => {
    const { openConfirmDialog } = await import("/static/js/app/dialogs.js");
    // biome-ignore lint/suspicious/noExplicitAny: test-only handle for the pending chain
    (window as any).chainedConfirmations = (async () => {
      if (!(await openConfirmDialog({ title: "First question", confirmLabel: "Go on" }))) {
        return "first-declined";
      }
      return (await openConfirmDialog({ title: "Second question", confirmLabel: "Really" }))
        ? "both-confirmed"
        : "second-declined";
    })();
  });

  const dialog = () => page.locator("[data-vm-action-dialog]").last();
  await expect(dialog().getByRole("heading", { name: "First question" })).toBeVisible();
  await dialog().locator("[data-confirm-yes]").click();
  await expect(dialog().getByRole("heading", { name: "Second question" })).toBeVisible();
  await dialog().locator("[data-confirm-yes]").click();

  // biome-ignore lint/suspicious/noExplicitAny: test-only handle for the pending chain
  expect(await page.evaluate(() => (window as any).chainedConfirmations)).toBe("both-confirmed");
  // A closed modal leaves the document, so the stale close event has nowhere to land.
  await expect(page.locator("[data-vm-action-dialog]")).toHaveCount(0);
});

test("two chained fields dialogs validate and own separate elements", async ({ page }) => {
  await page.evaluate(async () => {
    const { openFieldsDialog } = await import("/static/js/app/dialogs.js");
    // biome-ignore lint/suspicious/noExplicitAny: test-only handle for the pending chain
    (window as any).chainedFieldDialogs = (async () => {
      const first = await openFieldsDialog({
        title: "Forced retirement",
        body: "<p>Trusted consequence summary</p>",
        confirmLabel: "Continue",
        danger: true,
        fields: [
          {
            name: "permanent_key",
            label: "Permanent key",
            required: true,
            validate: (value: string) => (value === "e2e" ? "" : "Enter e2e to continue."),
          },
          {
            name: "reason",
            label: "Reason",
            type: "textarea",
            required: true,
            maxLength: 40,
            validate: (value: string) => (value.length >= 8 ? "" : "Give a more specific reason."),
          },
        ],
      });
      if (!first) return { first: null, second: null };

      const second = await openFieldsDialog({
        title: "Are you really sure?",
        confirmLabel: "Retire permanently",
        cancelLabel: "Go back",
        danger: true,
        swapActions: true,
        distinguishDismiss: true,
        fields: [{ name: "final_key", label: "Permanent key again", required: true }],
      });
      return { first, second };
    })();
  });

  const dialog = () => page.locator("[data-vm-action-dialog]").last();
  await expect(dialog().getByRole("heading", { name: "Forced retirement" })).toBeVisible();
  await expect(dialog().getByText("Trusted consequence summary")).toBeVisible();
  const firstElement = await dialog().elementHandle();
  expect(firstElement).not.toBeNull();

  await dialog().getByLabel("Permanent key").fill("wrong");
  await dialog().getByLabel("Reason").fill("short");
  await dialog().getByRole("button", { name: "Continue", exact: true }).click();
  await expect(dialog().getByText("Enter e2e to continue.")).toBeVisible();
  await expect(dialog().getByText("Give a more specific reason.")).toBeVisible();

  await dialog().getByLabel("Permanent key").fill("  e2e  ");
  await dialog().getByLabel("Reason").fill("  Cluster no longer exists.  ");
  await dialog().getByRole("button", { name: "Continue", exact: true }).click();

  await expect(dialog().getByRole("heading", { name: "Are you really sure?" })).toBeVisible();
  expect(await dialog().evaluate((current, previous) => current === previous, firstElement)).toBe(false);
  await expect(dialog().locator(".form-actions button")).toHaveText(["Go back", "Retire permanently"]);
  await dialog().getByLabel("Permanent key again").fill("e2e");
  await dialog().getByRole("button", { name: "Retire permanently", exact: true }).click();

  // biome-ignore lint/suspicious/noExplicitAny: test-only handle for the pending chain
  expect(await page.evaluate(() => (window as any).chainedFieldDialogs)).toEqual({
    first: { permanent_key: "e2e", reason: "Cluster no longer exists." },
    second: { outcome: "confirm", values: { final_key: "e2e" } },
  });
  await expect(page.locator("[data-vm-action-dialog]")).toHaveCount(0);
});

test("fields dialogs distinguish declining from dismissing", async ({ page }) => {
  await page.evaluate(async () => {
    const { openFieldsDialog } = await import("/static/js/app/dialogs.js");
    // biome-ignore lint/suspicious/noExplicitAny: test-only handle for the pending chain
    (window as any).fieldDialogOutcomes = (async () => {
      const dismissed = await openFieldsDialog({
        title: "Dismiss this question",
        distinguishDismiss: true,
        fields: [{ name: "value", label: "Value" }],
      });
      const declined = await openFieldsDialog({
        title: "Decline this question",
        distinguishDismiss: true,
        fields: [{ name: "value", label: "Value" }],
      });
      return { dismissed, declined };
    })();
  });

  const dialog = () => page.locator("[data-vm-action-dialog]").last();
  await expect(dialog().getByRole("heading", { name: "Dismiss this question" })).toBeVisible();
  await dialog().getByRole("button", { name: "Close" }).click();
  await expect(dialog().getByRole("heading", { name: "Decline this question" })).toBeVisible();
  await dialog().locator("[data-fields-no]").click();

  // biome-ignore lint/suspicious/noExplicitAny: test-only handle for the pending chain
  expect(await page.evaluate(() => (window as any).fieldDialogOutcomes)).toEqual({
    dismissed: { outcome: "dismiss", values: null },
    declined: { outcome: "decline", values: null },
  });
});

test("storage consumer release lists its exact impact before submitting acknowledgement", async ({ page }) => {
  await page.goto("/clusters/e2e/datastores/e2e-nfs/summary/", { waitUntil: "load" });

  let submitted = "";
  await page.route("**/clusters/e2e/storage-consumers/release/", async (route) => {
    submitted = route.request().postData() || "";
    await route.fulfill({ status: 204 });
  });

  await page.getByRole("button", { name: "Release all consumers for E2E cluster" }).click();
  const dialog = page.locator("[data-vm-action-dialog]");
  await expect(dialog.getByRole("heading", { name: "Release all consumers for E2E cluster?" })).toBeVisible();
  await expect(dialog.getByRole("cell", { name: "E2E shared storage" })).toBeVisible();
  await expect(dialog.getByRole("cell", { name: "pve1" })).toBeVisible();
  await expect(dialog.getByText("Consumers belonging to other managed clusters")).toBeVisible();

  await dialog.getByRole("button", { name: "Release 1 consumer", exact: true }).click();
  await expect.poll(() => submitted).toMatch(/name="confirm_release"[\s\S]*\r?\nyes/);
});

test("forced cluster retirement uses impact fields and a separate swapped final dialog", async ({ page }) => {
  await page.goto("/clusters/e2e/connection/", { waitUntil: "load" });

  let finalSubmission = "";
  await page.route("**/clusters/e2e/connection/action/", async (route) => {
    const posted = route.request().postData() || "";
    if (posted.includes('name="action"') && posted.includes("retirement-preflight")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          ready: true,
          confirmation: "signed-e2e-confirmation",
          cluster: { key: "e2e", display_name: "E2E cluster" },
          impact: {
            mode: "forced",
            identity_verification: "skipped",
            endpoint: "",
            counts: {
              schedules: 2,
              schedule_runs_not_started: 1,
              schedule_runs_active: 1,
              current_projections: 2,
              history: 4,
              storage_definitions: 2,
              storage_consumers: 1,
              consoles_pending: 0,
              consoles_active: 1,
              provider_operations_queued: 1,
              provider_operations_running: 1,
              active_scans: 0,
            },
            storage_consumers: [
              {
                storage_id: "e2e-nfs",
                storage_name: "E2E shared storage",
                node: "pve1",
                url: "/clusters/e2e/datastores/e2e-nfs/summary/",
              },
            ],
            blockers: [],
          },
        }),
      });
      return;
    }
    finalSubmission = posted;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, mode: "forced", redirect_url: "/clusters/" }),
    });
  });

  await page.getByText("Need to retire a permanently unavailable site?").click();
  await page.getByRole("button", { name: "Force retire", exact: true }).click();

  const first = page.locator("[data-vm-action-dialog]").last();
  await expect(first.getByRole("heading", { name: "Force-retire cluster" })).toBeVisible();
  await expect(first.getByText("Skipped — forced retirement makes no provider request")).toBeVisible();
  await expect(first.getByText("Current guest projections")).toBeVisible();
  await expect(first.getByRole("link", { name: "E2E shared storage" })).toBeVisible();
  await first.evaluate((element) => {
    (window as Window & { retirementFirstDialog?: Element }).retirementFirstDialog = element;
  });

  await first.getByLabel("Type the permanent cluster key").fill("wrong");
  await first.getByLabel("Why is this site permanently unavailable?").fill("The site was decommissioned.");
  await first.getByRole("button", { name: "Site is permanently unavailable" }).click();
  await expect(first.getByText("Type e2e exactly.")).toBeVisible();
  await first.getByLabel("Type the permanent cluster key").fill("e2e");
  await first.getByRole("button", { name: "Site is permanently unavailable" }).click();

  const second = page.locator("[data-vm-action-dialog]").last();
  await expect(second.getByRole("heading", { name: "Are you really sure?" })).toBeVisible();
  expect(
    await second.evaluate(
      (element) => element !== (window as Window & { retirementFirstDialog?: Element }).retirementFirstDialog
    )
  ).toBe(true);
  await expect(second.locator(".form-actions button")).toHaveText(["Go back", "Force-retire permanently"]);
  await second.getByRole("button", { name: "Force-retire permanently" }).click();

  await expect(page).toHaveURL(/\/clusters\/$/);
  expect(finalSubmission).toContain("retire");
  expect(finalSubmission).toContain("signed-e2e-confirmation");
  expect(finalSubmission).toContain("The site was decommissioned.");
  expect(finalSubmission).toContain("permanent_unavailability_asserted");
});
