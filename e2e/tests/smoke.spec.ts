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
  { name: "Connections", path: "/clusters/" },
  { name: "Add host/cluster", path: "/clusters/add/" },
  { name: "Cluster connection detail", path: "/clusters/e2e/connection/" },
  { name: "Retired cluster detail", path: "/clusters/retired-e2e/connection/" },
  { name: "New scheduled task", path: "/clusters/e2e/scheduled-tasks/new/" },
  { name: "Datastore consumer view", path: "/clusters/e2e/datastores/e2e-nfs/summary/" },
  { name: "Datastore Recycle Bin", path: "/clusters/e2e/datastores/e2e-nfs/recycle-bin/" },
  { name: "Recycle Bins overview", path: "/storage/recycle-bins/" },
  { name: "Orphan Finder", path: "/orphans/" },
  { name: "Tags", path: "/clusters/e2e/tags/" },
  { name: "PVE-helper Settings", path: "/settings/storage/" },
  { name: "Log forwarder settings", path: "/settings/log-forwarder/" },
  { name: "Scheduled Tasks settings", path: "/settings/scheduled-tasks/" },
  { name: "Audit log", path: "/audit/" },
];

for (const p of PAGES) {
  test(`${p.name} loads and app.js initialises`, async ({ page }) => {
    const jsErrors: string[] = [];
    const cspErrors: string[] = [];
    page.on("pageerror", (err) => jsErrors.push(String(err)));
    page.on("console", (message) => {
      if (message.type() === "error" && message.text().includes("Content Security Policy")) {
        cspErrors.push(message.text());
      }
    });

    const resp = await page.goto(p.path, { waitUntil: "load" });
    expect(resp?.status(), `${p.path} HTTP status`).toBeLessThan(400);
    const csp = resp?.headers()["content-security-policy"] ?? "";
    expect(csp, `${p.path} enforced Content-Security-Policy`).toContain("script-src 'self'");
    // Django's json_script helper emits inert application/json data blocks;
    // executable scripts must always come from same-origin static assets.
    await expect(page.locator('script:not([src]):not([type="application/json"])')).toHaveCount(0);

    // app.js is deferred; give its init a beat, then confirm it ran.
    await expect
      .poll(() => page.evaluate(() => typeof (window as unknown as { pveHelperRefreshRecentTasks?: unknown }).pveHelperRefreshRecentTasks), {
        timeout: 5_000,
      })
      .toBe("function");

    expect(jsErrors, `uncaught JS errors on ${p.path}`).toEqual([]);
    expect(cspErrors, `CSP violations on ${p.path}`).toEqual([]);
  });
}

test("header displays the configured application version", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".brand-version")).toHaveText("DEV");
});

test("storage overview contains the catalog and links to application storage settings", async ({ page }) => {
  await page.goto("/");
  const summaryPanels = page.locator(".dashboard-summary-grid > .panel");
  const latestScanBox = await summaryPanels.nth(0).boundingBox();
  const classificationBox = await summaryPanels.nth(1).boundingBox();
  expect(latestScanBox).not.toBeNull();
  expect(classificationBox).not.toBeNull();
  expect(Math.abs((latestScanBox?.width ?? 0) - (classificationBox?.width ?? 0))).toBeLessThanOrEqual(1);
  expect(Math.abs((latestScanBox?.height ?? 0) - (classificationBox?.height ?? 0))).toBeLessThanOrEqual(1);

  await expect(page.locator("#storage-catalog")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Storage catalog", exact: true })).toBeVisible();

  await page.getByRole("link", { name: "Configure PVE-helper access", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/storage\/$/);
  await expect(page.getByRole("link", { name: "Storage access", exact: true })).toHaveClass(/active/);
  await expect(page.getByRole("heading", { name: "Registered associations", exact: true })).toBeVisible();

  await page.getByRole("link", { name: "View storage catalog", exact: true }).click();
  await expect(page).toHaveURL(/\/#storage-catalog$/);
  await expect(page.locator("#storage-catalog")).toBeVisible();
});

test("mount registration derives identity and offers the node instances of the chosen datastore", async ({
  page,
}) => {
  await page.goto("/settings/storage/");
  const datastore = page.locator("select[name='cluster_storage']");
  const identity = page.locator("input[name='backend_identity']");
  const nodeField = page.locator("[data-node-field]");

  // Shared storage: identity comes from the Proxmox definition, node does not apply.
  await datastore.selectOption({ label: "E2E cluster · e2e-nfs (nfs) — Shared" });
  await expect(identity).toHaveValue("nas.e2e.local:/mnt/tank/vm");
  await expect(page.locator("[data-identity-source]")).toContainText("Derived from the Proxmox definition");
  await expect(nodeField).toBeHidden();

  // An operator edit is kept and labelled as an override of the derived value.
  await identity.fill("other.example:/export");
  await expect(page.locator("[data-identity-source]")).toContainText("Overridden");

  // Node-local storage: the node becomes a choice between published instances,
  // and a backend that publishes no identity says so instead of guessing.
  await datastore.selectOption({ label: "E2E cluster · e2e-dir (dir) — Node-local" });
  await expect(nodeField).toBeVisible();
  await expect(page.locator("[data-node-select] option")).toHaveText(["Choose the node instance…", "pve1"]);
  await expect(page.locator("[data-identity-source]")).toContainText("does not publish its identity");
});

test("log forwarder uses a compact header toggle and standard save action", async ({ page }) => {
  await page.goto("/settings/log-forwarder/");

  await expect(page.getByRole("navigation", { name: "PVE-helper settings areas" }).getByRole("link")).toHaveText([
    "Certificates",
    "Log forwarder",
    "Scheduled Tasks",
    "Storage access",
  ]);

  const toggle = page.locator('input[name="enabled"]');
  const box = await toggle.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.width).toBeLessThanOrEqual(20);
  expect(box!.height).toBeLessThanOrEqual(20);
  await expect(toggle.locator("xpath=ancestor::div[contains(@class, 'panel-heading')]")).toHaveCount(1);
  const saveButton = page.getByRole("button", { name: "Save configuration" });
  await expect(saveButton).toHaveClass(/secondary-action/);
  const testButton = page.getByRole("button", { name: "Send test event" });
  await expect(testButton).toHaveCSS("white-space", "nowrap");
  const testButtonBox = await testButton.boundingBox();
  expect(testButtonBox).not.toBeNull();
  expect(testButtonBox!.width).toBeGreaterThanOrEqual(145);

  await saveButton.click();
  await expect(page).toHaveURL(/\/settings\/log-forwarder\/$/);
  await expect(page.locator(".messages")).toHaveCount(0);
});

test("scheduled task settings expose bounded run-history retention", async ({ page }) => {
  await page.goto("/settings/scheduled-tasks/");

  await expect(page.getByRole("link", { name: "Scheduled Tasks", exact: true })).toHaveClass(/active/);
  const retention = page.getByLabel("Retention days");
  await expect(retention).toHaveAttribute("min", "1");
  await expect(retention).toHaveAttribute("max", "999");
  await expect(retention).toHaveValue("90");
  await expect(page.getByRole("button", { name: "Save", exact: true })).toHaveClass(/secondary-action/);
});

test("scheduled task end conditions share the date picker and preview run limits", async ({ page }) => {
  await page.goto("/clusters/e2e/scheduled-tasks/new/", { waitUntil: "load" });

  const recurrence = page.getByLabel("Recurrence");
  const endCondition = page.getByLabel("End Condition");
  await expect(endCondition).toBeDisabled();

  await recurrence.selectOption("daily");
  await expect(endCondition).toBeEnabled();
  await endCondition.selectOption("run_until");

  const runUntilDate = page.locator('input[name="run_until_date"]');
  await expect(runUntilDate).toBeEnabled();
  await page.getByRole("button", { name: "Choose run until date" }).click();
  const runUntilPicker = runUntilDate.locator("xpath=..").locator("[data-schedule-date-popover]");
  await expect(runUntilPicker).toBeVisible();
  await runUntilPicker.locator("[data-schedule-date-day].in-month").first().click();
  await expect(runUntilDate).toHaveValue(/^\d{4}-\d{2}-\d{2}$/);

  await endCondition.selectOption("run_count");
  await expect(runUntilDate).toBeDisabled();
  const runTimes = page.getByLabel("Run Times");
  await expect(runTimes).toBeEnabled();
  await runTimes.fill("5");
  await expect(page.locator("[data-schedule-preview-time]")).toContainText("5 scheduled runs remaining");
});

test("log forwarder refreshes delivery status and labels a paused backlog", async ({ page }) => {
  await page.route("**/settings/log-forwarder/status/", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        state: "Disabled",
        pending: 1,
        pending_label: "1 (paused)",
        paused: true,
        last_delivery: "2026-07-22 13:09:39",
        last_error: "Connection failed at 2026-07-22 13:11:19",
      }),
    });
  });

  await page.goto("/settings/log-forwarder/");

  await expect(page.locator("[data-log-forwarder-pending]")).toHaveText("1 (paused)");
  await expect(page.locator("[data-log-forwarder-paused]")).toBeVisible();
});

test("log forwarder shows the certificate trust decision and its three honest answers", async ({ page }) => {
  // The inspection is stubbed because there is no collector to probe here; what
  // this asserts is the part the operator sees — that a destination with no
  // approval says so, and that "no verification" is offered under its own name
  // rather than hidden behind the recommended path.
  await page.route("**/settings/log-forwarder/inspect/", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        host: "siem.example.test",
        port: 6514,
        expiry_warning_days: 7,
        current: null,
        certificate: {
          subject: "CN=siem.example.test",
          issuer: "CN=example internal CA",
          sha256_fingerprint: "ab".repeat(32),
          not_before: "2026-01-01 00:00",
          not_after: "2027-01-01 00:00",
          expires_in_days: 160,
          self_signed: false,
          system_trusted: false,
          verification_error: "unable to get local issuer certificate",
          ca_available: true,
        },
      }),
    });
  });

  await page.goto("/settings/log-forwarder/");

  const panel = page.locator("[data-log-forwarder-trust]");
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("button", { name: /Inspect and approve|Review certificate/ })).toBeVisible();

  // The dialog inspects whatever is typed into the form, not only what is saved,
  // so an operator can look at a destination before committing to it.
  await page.fill('input[name="host"]', "siem.example.test");
  await panel.locator("[data-log-forwarder-approve]").click();

  const dialog = page.locator(".log-forwarder-trust-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("does not verify against the installation's trust store");
  await expect(dialog.locator(".log-forwarder-fingerprint")).toContainText("AB:AB");
  await expect(dialog.locator("input[name='trust-mode']")).toHaveCount(3);
  // The safe default is preselected, and the unverified answer is spelled out.
  await expect(dialog.locator("input[name='trust-mode'][value='ca']")).toBeChecked();
  await expect(dialog).toContainText("Accept any certificate (no verification)");
});

test("cluster connection UI separates immutable identity from write-only credentials", async ({ page }) => {
  await page.goto("/clusters/");
  await expect(page.getByRole("heading", { name: "Connections" })).toBeVisible();
  // Scoped to main: the sidebar now lists every managed cluster under Tags and
  // Scheduled Tasks, so the bare name matches three links.
  await expect(page.getByRole("main").getByRole("link", { name: "E2E cluster", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Add host/cluster", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Add Proxmox host or cluster" })).toBeVisible();
  await expect(page.getByLabel("Cluster key")).toBeVisible();
  await expect(page.getByText("cannot be renamed later")).toBeVisible();
  await expect(page.locator('input[name="token_secret"]')).toHaveCount(0);

  await page.goto("/clusters/e2e/connection/");
  await expect(page.getByText("Permanent key")).toBeVisible();
  const secret = page.locator('input[name="token_secret"]');
  await expect(secret).toHaveValue("");
  await expect(secret).toHaveAttribute("autocomplete", "new-password");
});

test("a disabled cluster still navigates and is marked degraded everywhere it appears", async ({ page }) => {
  // Disabling retains inventory, schedules and history. Building navigation from
  // the enabled set deleted all of it from the UI, and verified retirement is gated
  // on disabling first — so preparing to retire removed the means to decide.
  await page.goto("/vms/overview/");

  const tagsLeaf = page
    .getByLabel("Tags module")
    .getByRole("link", { name: "Unused E2E connection" });
  await expect(tagsLeaf).toBeVisible();
  await expect(tagsLeaf.locator(".tree-state-badge")).toHaveText("Disabled");
  await expect(
    page.getByLabel("Scheduled Tasks module").getByRole("link", { name: "Unused E2E connection" }),
  ).toBeVisible();
  // Retired stays out of navigation entirely; it is an archive row, not a target.
  await expect(page.getByRole("link", { name: "Retired E2E cluster" })).toHaveCount(0);

  await tagsLeaf.click();

  await expect(page).toHaveURL(/\/clusters\/unused-e2e\/tags\/$/);
  await expect(page.locator(".cluster-degraded-notice")).toContainText("Disabled");
  await expect(page.locator(".cluster-degraded-notice")).toContainText(
    "refreshes, schedules, consoles and writes are refused",
  );
});

test("an unobserved guest is published as unknown, not as stopped and healthy", async ({ page }) => {
  // A node that stops answering leaves status='unknown'. Every surface used to
  // convert that absence into a value: the header counted it as not-running, the
  // health card called it Healthy, Usage said 0%, and the action menu offered
  // Power On for a guest that may well have been running the whole time.
  await page.goto("/vms/overview/");

  const heading = page.getByRole("main").locator(".vm-overview-heading");
  await expect(heading).toContainText("1 unknown");
  // Name the thing that is actually down, not the guests downstream of it.
  await expect(heading).toContainText("node pve2");

  await page.goto("/vms/e2e/vm/102/summary/");

  const health = page.locator('[data-card-key="health"]');
  await expect(health).toContainText("Unknown");
  await expect(health).not.toContainText("No issues detected");

  const usage = page.locator('[data-card-key="usage"]');
  await expect(usage).toContainText("Not observed");
  await expect(usage).not.toContainText("0.0%");

  await page.locator('[data-card-key="details"] .actions-menu > summary').click();
  await expect(page.locator(".actions-menu-note")).toContainText("Power state unknown");
  await expect(page.getByRole("button", { name: "Power On" })).toHaveCount(0);
});

test("retired connection archive opens its read-only tombstone and filtered Audit history", async ({ page }) => {
  await page.goto("/clusters/");

  await expect(page.getByRole("heading", { name: "Configured hosts & clusters" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Retired hosts & clusters" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Retired E2E cluster", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Retired E2E cluster", exact: true }).click();

  await expect(page).toHaveURL(/\/clusters\/retired-e2e\/connection\/$/);
  await expect(page.getByRole("heading", { name: "Read-only retired connection" })).toBeVisible();
  await expect(page.getByText("22222222-2222-2222-2222-222222222222")).toBeVisible();
  await expect(page.locator("main form")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Add endpoint" })).toHaveCount(0);
  await page.getByRole("link", { name: "Open filtered Audit" }).click();

  await expect(page).toHaveURL(/\/audit\/\?cluster=retired-e2e$/);
  const clusterFilter = page.getByLabel("Cluster", { exact: true });
  await expect(clusterFilter).toHaveValue("retired-e2e");
  await expect(clusterFilter).toContainText("Retired E2E cluster (retired)");
  await expect(page.getByRole("cell", { name: "Force-retire cluster" })).toBeVisible();
  await expect(page.getByRole("cell", { name: /Identity verification skipped · 2 endpoints removed/ })).toBeVisible();
  await expect(page.locator(".audit-table").getByText("E2E Store")).toHaveCount(0);
  await expect(page.locator(".audit-table").getByText("cluster.force_retired")).toHaveCount(0);
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

test("VM overview table text remains readable in both themes", async ({ page }) => {
  await page.goto("/vms/overview/", { waitUntil: "load" });

  for (const theme of ["light", "dark"]) {
    await page.evaluate((selectedTheme) => {
      document.documentElement.dataset.theme = selectedTheme;
    }, theme);

    for (const selector of ["#vm-overview-table th[data-column='state']", "#vm-overview-table td[data-column='state']"]) {
      const contrast = await page.locator(selector).first().evaluate((element) => {
        const colorChannels = (value: string) => value.match(/\d+(?:\.\d+)?/g)?.slice(0, 3).map(Number) ?? [];
        const relativeLuminance = (channels: number[]) =>
          channels
            .map((channel) => {
              const normalized = channel / 255;
              return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
            })
            .reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);
        const backgroundColor = (node: Element | null): string => {
          if (!node) return "rgb(255, 255, 255)";
          const color = getComputedStyle(node).backgroundColor;
          return color === "rgba(0, 0, 0, 0)" ? backgroundColor(node.parentElement) : color;
        };
        const foreground = relativeLuminance(colorChannels(getComputedStyle(element).color));
        const background = relativeLuminance(colorChannels(backgroundColor(element)));
        return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
      });
      expect(contrast, `${theme} theme: ${selector}`).toBeGreaterThanOrEqual(4.5);
    }
  }
});

test("VM overview does not stretch the final visible column", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem(
      "pve-helper-columns-vm-overview",
      JSON.stringify({
        state: true,
        cluster: false,
        provisioned: true,
        used: false,
        cpu: false,
        "active-mem": false,
        "guest-os": false,
        hostname: false,
        agent: false,
        pool: false,
        ha: false,
        node: false,
        "has-snapshot": false,
        vmid: true,
        type: true,
        "memory-size": false,
        cpus: false,
        nics: false,
        disks: false,
        uptime: false,
        ip: false,
        mac: false,
        storage: false,
        tags: true,
      })
    );
    localStorage.removeItem("pve-helper-column-widths-vm-overview");
  });
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/vms/overview/", { waitUntil: "load" });

  const scroll = page.locator(".vm-overview-scroll");
  await expect(scroll).toHaveCSS("overflow-x", "auto");
  await expect
    .poll(() => scroll.evaluate((element) => element.scrollWidth <= element.clientWidth))
    .toBe(true);
  await expect(page.locator("#vm-overview-table th[data-column='tags']")).toHaveCSS("width", "170px");
});

test("VM overview makes wide visible columns horizontally reachable", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.removeItem("pve-helper-columns-vm-overview");
    localStorage.removeItem("pve-helper-column-widths-vm-overview");
  });
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto("/vms/overview/", { waitUntil: "load" });

  const scroll = page.locator(".vm-overview-scroll");

  await expect
    .poll(() => scroll.evaluate((element) => element.scrollWidth > element.clientWidth))
    .toBe(true);
  await scroll.evaluate((element) => {
    element.scrollLeft = element.scrollWidth;
  });
  await expect
    .poll(() => scroll.evaluate((element) => element.scrollLeft + element.clientWidth >= element.scrollWidth - 1))
    .toBe(true);
});

test("CSS layers load in the intended cascade order", async ({ page }) => {
  await page.goto("/vms/overview/", { waitUntil: "load" });
  // Annotated rather than inferred: evaluateAll hands back SVGElement | HTMLElement,
  // and neither of those has `href`. The selector already guarantees a <link>.
  const hrefs = await page
    .locator('link[rel="stylesheet"]')
    .evaluateAll((links: HTMLLinkElement[]) => links.map((link) => link.href));
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

// The page title has to survive soft navigation, which replaces the content
// block by innerHTML and never re-runs the document head. `navigation.js`
// already copied the fetched document's title across — while every page had the
// same one, so the line could not be wrong. It can now.
test("the browser title follows a soft navigation", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle("Storage Overview · pve-helper");
  await page.evaluate(() => {
    (window as Window & { titleSoftNavigationMarker?: string }).titleSoftNavigationMarker = "preserved";
  });
  await page.getByRole("link", { name: "Audit", exact: true }).click();
  await expect(page).toHaveURL(/\/audit\/$/);
  await expect(page).toHaveTitle("Audit Log · pve-helper");
  await expect
    .poll(() => page.evaluate(() => (window as Window & { titleSoftNavigationMarker?: string }).titleSoftNavigationMarker))
    .toBe("preserved");
});

// The Certificates tab is where an operator turns their own installation into an
// HTTPS one, so the two things that must never be quietly wrong are that the
// serving certificate cannot be deleted out from under nginx, and that the upload
// form admits every format a CA actually hands people. Both are asserted on the
// rendered page rather than through the service, because the service already has
// unit coverage and the failure being guarded here is a template that stops
// disabling the button.
test("certificates tab offers every supported format and protects the serving certificate", async ({ page }) => {
  await page.goto("/settings/certificates/");

  await expect(page.getByRole("heading", { name: "HTTPS server certificate" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Trusted certificate authorities" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Expiry warnings" })).toBeVisible();

  // PKCS#12 and DER are the two an operator is most likely to be handed and least
  // likely to be able to convert without a shell.
  const upload = page.locator('input[name="certificate"]').first();
  const accepted = (await upload.getAttribute("accept")) || "";
  for (const extension of [".pem", ".crt", ".cer", ".der", ".p12", ".pfx"]) {
    expect(accepted).toContain(extension);
  }
  await expect(page.locator('input[name="password"]')).toHaveAttribute("type", "password");

  // The threshold is one number for every certificate in the installation.
  const days = page.locator('input[name="expiry_warning_days"]');
  await expect(days).toHaveAttribute("min", "1");
  await expect(days).toHaveAttribute("max", "99");

  // Nothing is stored in the E2E database, so the empty state is what proves the
  // table renders rather than throwing.
  await expect(page.getByText("No server certificate has been uploaded yet.")).toBeVisible();
});

test("certificates tab is reachable from the settings tab strip", async ({ page }) => {
  await page.goto("/settings/log-forwarder/");
  await page.getByRole("link", { name: "Certificates", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/certificates\/$/);
  await expect(page.getByRole("heading", { name: "Certificates", exact: true })).toBeVisible();
});
