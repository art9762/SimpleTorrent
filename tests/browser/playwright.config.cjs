const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: ".",
  workers: 1,
  use: { baseURL: "http://127.0.0.1:8766", trace: "retain-on-failure" },
  projects: [
    { name: "desktop-light", use: { ...devices["Desktop Chrome"], colorScheme: "light" } },
    { name: "desktop-dark", use: { ...devices["Desktop Chrome"], colorScheme: "dark" } },
    { name: "mobile-light", use: { ...devices["Pixel 7"], colorScheme: "light" } },
    { name: "mobile-dark", use: { ...devices["Pixel 7"], colorScheme: "dark" } },
  ],
  webServer: {
    command: `${process.env.ST_TEST_PYTHON || "python3"} ../browser_server.py`,
    url: "http://127.0.0.1:8766",
  },
});
