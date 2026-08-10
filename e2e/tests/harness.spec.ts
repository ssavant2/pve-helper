import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { test, expect } from "@playwright/test";

import { guestRow, openGuestFromTree } from "./helpers/navigation";

test("representative fixture exposes duplicate guest identity across cluster keys", async ({ page }) => {
  await page.goto("/vms/overview/", { waitUntil: "load" });

  await expect(guestRow(page, { clusterKey: "e2e", objectType: "vm", vmid: 100 })).toHaveCount(1);
  await expect(
    guestRow(page, { clusterKey: "standalone-e2e", objectType: "vm", vmid: 100 }),
  ).toHaveCount(1);

  await openGuestFromTree(page, { clusterKey: "standalone-e2e", objectType: "vm", vmid: 100 });
  await expect(page.getByRole("heading", { name: /standalone-duplicate-vm-100/ })).toBeVisible();
});

test("direct navigation is limited to approved non-object entry routes", () => {
  const testDirectory = __dirname;
  const allowed = new Set([
    "/",
    "/audit/",
    "/clusters/",
    "/clusters/add/",
    "/orphans/",
    "/settings/certificates/",
    "/settings/log-forwarder/",
    "/settings/scheduled-tasks/",
    "/settings/storage/",
    "/storage/recycle-bins/",
    "/vms/",
    "/vms/overview/",
  ]);
  const gotoArgument = /page\.goto\(\s*([^,\n)]+)/g;
  const offenders: string[] = [];
  const sources = readdirSync(testDirectory)
    .filter((name) => name.endsWith(".spec.ts"))
    .sort()
    .map((name) => [name, join(testDirectory, name)] as const);
  sources.push(["helpers/navigation.ts", join(testDirectory, "helpers", "navigation.ts")]);

  for (const [filename, path] of sources) {
    const source = readFileSync(path, "utf8");
    for (const match of source.matchAll(gotoArgument)) {
      const argument = match[1].trim();
      const literal = argument.match(/^(["'])([^"']*)\1$/);
      const destination = literal?.[2];
      if (!destination || !allowed.has(destination)) {
        const line = source.slice(0, match.index).split("\n").length;
        offenders.push(`${filename}:${line}: ${argument}`);
      }
    }
  }

  expect(
    offenders,
    "page.goto must use an approved literal entry route; object journeys click rendered links",
  ).toEqual([]);
});

test("future infrastructure helper is cluster-qualified and never constructs an object URL", () => {
  const helper = readFileSync(join(__dirname, "helpers", "navigation.ts"), "utf8");

  expect(helper).toContain('data-infrastructure-kind="cluster"');
  expect(helper).toContain('data-infrastructure-kind="node"');
  expect(helper).toContain("data-cluster-key");
  expect(helper).toContain("data-node");
});
