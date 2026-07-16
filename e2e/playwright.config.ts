import { defineConfig, devices } from "@playwright/test";

// Dev-only. Targets the isolated e2e-web app (APP_REQUIRE_LOGIN=false, SQLite,
// PVE network disabled) brought up by docker-compose.tools.yml — never prod.
export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: true,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL || "http://e2e-web:8000",
    headless: true,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
