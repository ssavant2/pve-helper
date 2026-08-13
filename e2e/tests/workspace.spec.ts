import { expect, test } from "@playwright/test";

import { openInfrastructureObjectFromTree, openTreeLeaf } from "./helpers/navigation";

// 5a2A+B. The contract's acceptance is a click-through: reach every workspace
// object from `/` with zero typed URLs, at both required viewports. Typing a URL
// would pass even if the tree never rendered a link, which is the failure these
// tests exist to catch.

const VIEWPORTS = [
  { name: "1920x1080", width: 1920, height: 1080 },
  { name: "1366x768", width: 1366, height: 768 },
] as const;

for (const viewport of VIEWPORTS) {
  test(`cluster and node open from the tree at ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });

    const clusterResponse = await openInfrastructureObjectFromTree(page, {
      kind: "cluster",
      clusterKey: "e2e",
    });
    expect(clusterResponse.status()).toBe(200);
    await expect(page.getByRole("heading", { name: "E2E cluster", level: 1 })).toBeVisible();

    const nodeResponse = await openInfrastructureObjectFromTree(page, {
      kind: "node",
      clusterKey: "e2e",
      node: "pve1",
    });
    expect(nodeResponse.status()).toBe(200);
    await expect(page.getByRole("heading", { name: "pve1", level: 1 })).toBeVisible();
  });
}

test("a standalone host is a sibling of clusters, not nested under one", async ({ page }) => {
  await page.goto("/", { waitUntil: "load" });

  const hosts = page.locator('[data-tree-module="workspace-hosts"]');
  const clusters = page.locator('[data-tree-module="workspace-clusters"]');

  // A standalone host renders as a single node leaf -- it *is* its node, so there
  // is no separate connection row to look for.
  const host = '[data-cluster-key="standalone-e2e"]';
  await expect(hosts.locator(host)).toHaveCount(1);
  await expect(hosts.locator(host)).toHaveAttribute("data-infrastructure-kind", "node");
  await expect(clusters.locator(host)).toHaveCount(0);
  await expect(
    clusters.locator('[data-infrastructure-kind="cluster"][data-cluster-key="e2e"]'),
  ).toBeVisible();
});

test("a hidden node is absent from the tree and has no page", async ({ page }) => {
  // `e2e` runs the enrollment contract with pve2 as safety_only.
  await page.goto("/", { waitUntil: "load" });

  await expect(page.locator('[data-cluster-key="e2e"][data-node="pve1"]')).toBeVisible();
  await expect(page.locator('[data-cluster-key="e2e"][data-node="pve2"]')).toHaveCount(0);

  // A request, not a navigation: the assertion is about the *response* to a URL
  // the tree deliberately never offers, so the harness's click-through rule has
  // nothing to enforce here.
  const response = await page.request.get("/clusters/e2e/nodes/pve2/summary/");
  expect(response.status()).toBe(404);
});

test("a retired cluster is reachable through Connections but never the tree", async ({ page }) => {
  await page.goto("/", { waitUntil: "load" });

  await expect(page.locator('[data-cluster-key="retired-e2e"]')).toHaveCount(0);

  const response = await openTreeLeaf(page, "hosts-clusters", "Connections");
  expect(response.status()).toBe(200);
  await expect(page.getByRole("main")).toContainText("Retired E2E cluster");
});

test("member nodes render nested under their cluster, not as a flat list", async ({ page }) => {
  await page.goto("/", { waitUntil: "load" });

  const cluster = page.locator('[data-infrastructure-kind="cluster"][data-cluster-key="e2e"]');
  const node = page.locator('[data-infrastructure-kind="node"][data-cluster-key="e2e"][data-node="pve1"]');

  await expect(cluster).toBeVisible();
  await expect(node).toBeVisible();
  // The node sits inside its own tree-children under the cluster, so it is indented
  // past the cluster row rather than sharing its left edge.
  const clusterBox = await cluster.boundingBox();
  const nodeBox = await node.boundingBox();
  expect(clusterBox).not.toBeNull();
  expect(nodeBox).not.toBeNull();
  expect(nodeBox!.x).toBeGreaterThan(clusterBox!.x);
});

test("a cluster with members can be collapsed without losing its own link", async ({ page }) => {
  await page.goto("/", { waitUntil: "load" });

  const section = page.locator('[data-tree-module="workspace-object-e2e"]');
  const node = section.locator('[data-node="pve1"]');
  const link = section.locator('[data-infrastructure-kind="cluster"]');

  await expect(node).toBeVisible();
  await expect(link).toBeVisible();

  await section.locator("[data-tree-toggle]").click();

  await expect(node).toBeHidden();
  // The cluster itself stays reachable: the toggle is beside the link, not instead
  // of it.
  await expect(link).toBeVisible();
});

test("cluster Summary states its capacity coverage rather than a bare total", async ({ page }) => {
  await openInfrastructureObjectFromTree(page, { kind: "cluster", clusterKey: "e2e" });

  const capacity = page.getByRole("main").locator(".panel", { hasText: "Capacity" }).first();
  await expect(capacity).toContainText(/reporting/);

  const guests = await page.locator(".cluster-summary-guests").boundingBox();
  const nodes = await page.locator(".cluster-summary-nodes").boundingBox();
  expect(guests).not.toBeNull();
  expect(nodes).not.toBeNull();
  expect(Math.abs(nodes!.y - guests!.y)).toBeLessThanOrEqual(1);
  expect(nodes!.x - (guests!.x + guests!.width)).toBeGreaterThanOrEqual(12);
  expect(Math.abs(nodes!.width - guests!.width)).toBeLessThanOrEqual(1);
  await expect(page.locator(".cluster-workspace-page > .vs-object-header")).toHaveCSS("border-bottom-width", "0px");

  const nodeHeaderPositions = await page
    .locator(".cluster-summary-nodes-table th")
    .evaluateAll((headers) => headers.map((header) => header.getBoundingClientRect().x));
  expect(nodeHeaderPositions).toHaveLength(4);
  expect(nodeHeaderPositions.slice(1).every((position, index) => position - nodeHeaderPositions[index] >= 120)).toBe(
    true,
  );
});

test("node Summary shows its own runtime state, not the cluster's", async ({ page }) => {
  // `pve1` is current; the page must speak for that node alone.
  await openInfrastructureObjectFromTree(page, { kind: "node", clusterKey: "e2e", node: "pve1" });

  await expect(page.getByRole("heading", { name: "pve1", level: 1 })).toBeVisible();
  await expect(page.getByRole("main")).toContainText("Guests");
  await expect(page.getByRole("main")).toContainText("Node reference");

  const guests = await page.locator(".cluster-detail-grid .panel", { hasText: "Guests" }).boundingBox();
  const identity = await page.locator(".node-summary-identity").boundingBox();
  expect(guests).not.toBeNull();
  expect(identity).not.toBeNull();
  expect(Math.abs(identity!.y - guests!.y)).toBeLessThanOrEqual(1);
  expect(identity!.x - (guests!.x + guests!.width)).toBeGreaterThanOrEqual(12);
  expect(Math.abs(identity!.width - guests!.width)).toBeLessThanOrEqual(1);
  await expect(page.locator(".cluster-workspace-page > .vs-object-header")).toHaveCSS("border-bottom-width", "0px");

  const runtimeLine = await page.locator(".node-runtime-observation").boundingBox();
  const cpuLabel = await page.locator(".node-runtime-observation + .cluster-detail-list dt").first().boundingBox();
  expect(runtimeLine).not.toBeNull();
  expect(cpuLabel).not.toBeNull();
  expect(Math.abs(runtimeLine!.x - cpuLabel!.x)).toBeLessThanOrEqual(1);
  await expect(page.locator(".node-runtime-observation")).toHaveCSS("border-bottom-width", "0px");
  const runtimeFontSize = await page.locator(".node-runtime-observation").evaluate((element) => getComputedStyle(element).fontSize);
  await expect(page.locator(".node-runtime-observation + .cluster-detail-list dt").first()).toHaveCSS(
    "font-size",
    runtimeFontSize,
  );
});

test("the built tabs are links and the unbuilt ones state the intended shape", async ({ page }) => {
  await openInfrastructureObjectFromTree(page, { kind: "cluster", clusterKey: "e2e" });

  const tabs = page.locator(".vs-tabs");
  for (const built of ["Summary", "Hosts", "VMs"]) {
    await expect(tabs.getByRole("link", { name: built, exact: true })).toBeVisible();
  }
  await expect(tabs.locator("span.disabled", { hasText: "Datastores" })).toBeVisible();
  await expect(tabs.locator("span.disabled", { hasText: "Monitor" })).toBeVisible();
});

test("the tables are reachable by clicking their tabs, and link into Module 3", async ({ page }) => {
  await openInfrastructureObjectFromTree(page, { kind: "cluster", clusterKey: "e2e" });

  await page.locator(".vs-tabs").getByRole("link", { name: "Hosts", exact: true }).click();
  await expect(page.getByRole("main")).toContainText("pve1");
  // A hidden node must not appear in the Hosts table either.
  await expect(page.getByRole("main")).not.toContainText("pve2");

  await page.locator(".vs-tabs").getByRole("link", { name: "VMs", exact: true }).click();
  await expect(page.locator(".vm-overview-page")).toBeVisible();
  await expect(page.getByPlaceholder("Quick Filter")).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Provisioned Space", exact: true })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Guest OS", exact: true })).toBeVisible();
  await expect(page.locator(".vm-overview-page .cluster-filter")).toHaveCount(0);
  await expect
    .poll(() =>
      page
        .locator("[data-vm-overview-row]")
        .evaluateAll((rows) => rows.every((row) => (row as HTMLElement).dataset.guestCluster === "e2e")),
    )
    .toBe(true);
  const guestLink = page.getByRole("main").locator('a[href^="/vms/e2e/"]').first();
  await expect(guestLink).toBeVisible();
});

test("the node VMs tab reuses Overview and stays locked to that node", async ({ page }) => {
  await openInfrastructureObjectFromTree(page, { kind: "node", clusterKey: "e2e", node: "pve1" });
  await page.locator(".vs-tabs").getByRole("link", { name: "VMs", exact: true }).click();

  await expect(page.locator(".vm-overview-page")).toBeVisible();
  await expect
    .poll(() =>
      page
        .locator("[data-vm-overview-row]")
        .evaluateAll((rows) => rows.length > 0 && rows.every((row) => row.querySelector('[data-column="node"]')?.textContent?.trim() === "pve1")),
    )
    .toBe(true);
});
