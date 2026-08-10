import { expect, type Locator, type Page, type Response } from "@playwright/test";

export type GuestObjectRef = {
  clusterKey: string;
  objectType: "vm" | "ct";
  vmid: number;
};

export type InfrastructureObjectRef =
  | { kind: "cluster"; clusterKey: string }
  | { kind: "node"; clusterKey: string; node: string };

function attributeValue(value: string | number): string {
  return String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

async function followRenderedLink(page: Page, link: Locator): Promise<Response> {
  await expect(link).toBeVisible();
  const href = await link.getAttribute("href");
  expect(href, "tree/object links must publish their destination").toBeTruthy();
  const destination = new URL(href!, page.url()).toString();
  const responsePromise = page.waitForResponse((response) => response.url() === destination);
  await link.click();
  const response = await responsePromise;
  await expect(page).toHaveURL(destination);
  return response;
}

async function moduleTree(page: Page, moduleName: string): Promise<Locator> {
  const module = page.locator(`[data-tree-module="${attributeValue(moduleName)}"]`).first();
  await expect(module).toBeVisible();
  const toggle = module.locator(":scope > [data-tree-toggle]");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") {
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
  }
  return module;
}

export async function openTreeLeaf(
  page: Page,
  moduleName: string,
  accessibleName: string,
): Promise<Response> {
  // Every object journey begins at an ordinary zero-argument page, then follows
  // the application tree.  Tests never construct the destination URL.
  await page.goto("/", { waitUntil: "load" });
  const module = await moduleTree(page, moduleName);
  return followRenderedLink(page, module.getByRole("link", { name: accessibleName, exact: true }));
}

export function guestRow(page: Page, ref: GuestObjectRef): Locator {
  return page.locator(
    `[data-vm-overview-row][data-guest-cluster="${attributeValue(ref.clusterKey)}"]` +
      `[data-guest-type="${attributeValue(ref.objectType)}"][data-guest-vmid="${ref.vmid}"]`,
  );
}

export async function openGuestFromTree(page: Page, ref: GuestObjectRef): Promise<Response> {
  await openTreeLeaf(page, "vms", "Inventory");
  return followRenderedLink(page, guestRow(page, ref));
}

export async function openConnectionFromTree(page: Page, clusterKey: string): Promise<Response> {
  await openTreeLeaf(page, "clusters", "Connections");
  const row = page.getByRole("main").locator("tbody tr").filter({
    has: page.locator(
      `td:nth-child(2)[data-sort-value="${attributeValue(clusterKey)}"]`,
    ),
  });
  await expect(
    row,
    `the permanent cluster key ${clusterKey} must identify exactly one rendered connection row`,
  ).toHaveCount(1);
  return followRenderedLink(page, row.locator("td").first().getByRole("link"));
}

export async function openTagInventoryFromTree(
  page: Page,
  clusterDisplayName: string,
): Promise<Response> {
  return openTreeLeaf(page, "tags", clusterDisplayName);
}

export async function openTagFromTree(
  page: Page,
  clusterDisplayName: string,
  tag: string,
): Promise<Response> {
  await openTagInventoryFromTree(page, clusterDisplayName);
  return followRenderedLink(
    page,
    page.locator("#tags-table").getByRole("link", { name: tag, exact: true }),
  );
}

export async function openScheduledTaskFormFromTree(
  page: Page,
  clusterDisplayName: string,
): Promise<Response> {
  await openTreeLeaf(page, "scheduled-tasks", clusterDisplayName);
  return followRenderedLink(page, page.getByRole("link", { name: "New Scheduled Task", exact: true }));
}

export async function openDatastoreFromTree(
  page: Page,
  clusterKey: string,
  storageId: string,
): Promise<Response> {
  await page.goto("/", { waitUntil: "load" });
  const storage = await moduleTree(page, "storage");
  const cluster = storage
    .locator(`[data-tree-module="datastore-cluster-${attributeValue(clusterKey)}"]`)
    .first();
  await expect(cluster).toBeVisible();
  const toggle = cluster.locator(":scope > [data-tree-toggle]");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  return followRenderedLink(page, cluster.getByRole("link", { name: storageId, exact: true }).first());
}

export async function openDatastoreTabFromTree(
  page: Page,
  clusterKey: string,
  storageId: string,
  tab: string,
): Promise<Response> {
  await openDatastoreFromTree(page, clusterKey, storageId);
  return followRenderedLink(
    page,
    page.getByRole("navigation", { name: "Datastore views" }).getByRole("link", {
      name: tab,
      exact: true,
    }),
  );
}

export async function openInfrastructureObjectFromTree(
  page: Page,
  ref: InfrastructureObjectRef,
): Promise<Response> {
  // 5a2B will render these stable identities on its object links. Keeping the
  // helper here makes every later object-tab spec click the tree from day one.
  await page.goto("/", { waitUntil: "load" });
  const module = await moduleTree(page, "clusters");
  const selector =
    ref.kind === "cluster"
      ? `[data-infrastructure-kind="cluster"][data-cluster-key="${attributeValue(ref.clusterKey)}"]`
      : `[data-infrastructure-kind="node"][data-cluster-key="${attributeValue(ref.clusterKey)}"]` +
        `[data-node="${attributeValue(ref.node)}"]`;
  return followRenderedLink(page, module.locator(selector));
}
