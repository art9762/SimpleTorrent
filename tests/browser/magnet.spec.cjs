const { test, expect } = require("@playwright/test");
const { createHash } = require("node:crypto");
const { readFile } = require("node:fs/promises");

const hash = "0123456789abcdef0123456789abcdef01234567";
const uri = `magnet:?xt=urn:btih:${hash}&dn=Тестовый+magnet`;

test.beforeEach(async ({ page, request }) => {
  await request.post("/_test/reset");
  await page.goto("/");
  await page.getByLabel("Пароль доступа").fill("browser-test-password");
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Ваши загрузки" })).toBeVisible();
});

async function addMagnet(page) {
  await page.getByRole("button", { name: "Magnet-ссылка", exact: true }).click();
  await page.getByLabel("Magnet-ссылка (v1)").fill(uri);
  await page.getByRole("button", { name: "Добавить ссылку", exact: true }).click();
  await expect(page.locator("#magnet-dialog")).not.toBeVisible();
  await expect(page.locator("#torrent-list .status-label")).toHaveText("Получаем метаданные");
}

test("magnet admission, metadata waiting, filters, pause and completion", async ({ page, request }, testInfo) => {
  await addMagnet(page);
  await expect(page.locator("#ready-count")).toContainText("0");
  await expect(page.locator("#torrent-list")).toContainText("Размер определяется");
  await expect(page.locator("#torrent-list").getByRole("button", { name: "Забрать", exact: true })).toHaveCount(0);
  await page.locator('[data-filter="active"]').click();
  await expect(page.locator("#torrent-list .torrent-row")).toHaveCount(1);
  await page.locator('[data-action="files"]').first().click();
  await expect(page.locator("#files-subtitle")).toContainText("Список файлов и размер пока неизвестны");
  await expect(page.locator("#archive-link")).not.toBeVisible();
  await page.locator('#files-dialog [data-close="files-dialog"]').click();
  await page.locator('[data-action="pause"]').click();
  await page.locator('[data-filter="paused"]').click();
  await expect(page.locator("#torrent-list .status-label")).toHaveText("На паузе");
  await page.locator('[data-action="resume"]').click();
  await page.locator('[data-filter="all"]').click();
  await expect(page.locator("#torrent-list .status-label")).toHaveText("Получаем метаданные");
  await expect(page.locator("#toasts .toast")).toHaveCount(0, { timeout: 6000 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("magnet-metadata.png"), fullPage: true });
  await request.post(`/_test/complete/${hash}`);
  await expect(page.locator("#torrent-list .status-label")).toHaveText("Готово", { timeout: 8000 });
  await page.getByRole("button", { name: "Забрать", exact: true }).click();
  await expect(page.locator("#archive-link")).toBeVisible();
  const pending = page.waitForEvent("download");
  await page.getByRole("link", { name: "Скачать", exact: true }).click();
  const download = await pending;
  expect(await readFile(await download.path(), "utf8")).toBe("hello world");
});

test("invalid and duplicate magnet errors remain actionable", async ({ page }) => {
  await page.getByRole("button", { name: "Magnet-ссылка", exact: true }).click();
  await page.getByLabel("Magnet-ссылка (v1)").fill("magnet:?xt=urn:btih:invalid");
  await page.getByRole("button", { name: "Добавить ссылку", exact: true }).click();
  await expect(page.locator("#magnet-error")).toContainText("v1 btih");
  await page.getByLabel("Magnet-ссылка (v1)").fill(uri);
  await page.getByRole("button", { name: "Добавить ссылку", exact: true }).click();
  await expect(page.locator("#magnet-dialog")).not.toBeVisible();
  await page.getByRole("button", { name: "Magnet-ссылка", exact: true }).click();
  await page.getByLabel("Magnet-ссылка (v1)").fill(uri);
  await page.getByRole("button", { name: "Добавить ссылку", exact: true }).click();
  await expect(page.locator("#magnet-error")).toContainText("уже есть");
  await expect(page.locator("#magnet-submit")).toBeEnabled();
});

test("existing torrent upload still works", async ({ page }) => {
  const info = Buffer.concat([
    Buffer.from("d6:lengthi11e4:name9:hello.txt12:piece lengthi16384e6:pieces20:"),
    createHash("sha1").update("hello world").digest(), Buffer.from("e"),
  ]);
  await page.locator("#torrent-input").setInputFiles({ name: "sample.torrent", mimeType: "application/x-bittorrent", buffer: Buffer.concat([Buffer.from("d4:info"), info, Buffer.from("e")]) });
  await expect(page.locator("#torrent-list")).toContainText("hello.txt");
  await expect(page.locator("#torrent-list .status-label")).toHaveText("Скачивается");
});
