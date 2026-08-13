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

  // Qualify by kind: the object leaf and its node leaf both carry the cluster key.
  const objectLeaf = '[data-infrastructure-kind="cluster"][data-cluster-key="standalone-e2e"]';
  await expect(hosts.locator(objectLeaf)).toBeVisible();
  await expect(clusters.locator(objectLeaf)).toHaveCount(0);
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

test("cluster Summary states its capacity coverage rather than a bare total", async ({ page }) => {
  await openInfrastructureObjectFromTree(page, { kind: "cluster", clusterKey: "e2e" });

  const capacity = page.getByRole("main").locator(".panel", { hasText: "Capacity" }).first();
  await expect(capacity).toContainText(/reporting/);
});

test("node Summary shows its own runtime state, not the cluster's", async ({ page }) => {
  // `pve1` is current; the page must speak for that node alone.
  await openInfrastructureObjectFromTree(page, { kind: "node", clusterKey: "e2e", node: "pve1" });

  await expect(page.getByRole("heading", { name: "pve1", level: 1 })).toBeVisible();
  await expect(page.getByRole("main")).toContainText("Guests");
  await expect(page.getByRole("main")).toContainText("Node reference");
});

test("only Summary is an enabled tab, and the rest state the intended shape", async ({ page }) => {
  await openInfrastructureObjectFromTree(page, { kind: "cluster", clusterKey: "e2e" });

  const tabs = page.locator(".vs-tabs");
  await expect(tabs.getByRole("link", { name: "Summary" })).toBeVisible();
  await expect(tabs.locator("span.disabled", { hasText: "Datastores" })).toBeVisible();
  await expect(tabs.locator("span.disabled", { hasText: "Monitor" })).toBeVisible();
});
