import { existsSync } from "node:fs";

import { chromium } from "playwright-core";

const baseUrl = process.env.HARNESS_FRONTEND_URL ?? "http://127.0.0.1:5173";
const browserCandidates = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"
];
const executablePath = browserCandidates.find(existsSync);

if (!executablePath) {
  throw new Error("No local Edge or Chrome executable found for UI verification.");
}

const browser = await chromium.launch({
  executablePath,
  headless: true,
  args: ["--disable-gpu", "--no-first-run"]
});

const results = [];

try {
  for (const viewport of [
    { name: "desktop", width: 1440, height: 1000 },
    { name: "mobile", width: 390, height: 844 }
  ]) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: 1
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") {
        errors.push(message.text());
      }
    });

    await page.goto(`${baseUrl}/#/overview`, { waitUntil: "networkidle" });
    await page.getByText("固定 Loop，能力由插件装配").waitFor();
    await page
      .locator("span.status-pill:visible", { hasText: "Runtime 在线" })
      .first()
      .waitFor();
    await assertNoHorizontalOverflow(page, `${viewport.name}/overview`);

    await clickVisibleButton(page, "插件");
    await page.getByRole("heading", { name: "插件管理" }).waitFor();
    await page.getByText("安装功能包").waitFor();
    await page.getByLabel("插件分类").waitFor();
    await assertNoHorizontalOverflow(page, `${viewport.name}/plugins`);

    await clickVisibleButton(page, "能力");
    await page.getByRole("heading", { name: "能力目录" }).waitFor();
    await page.getByText("模型可见工具").waitFor();
    await page.getByRole("button", { name: /MCP/ }).click();
    await page.getByRole("checkbox").first().waitFor();
    await page.getByText("按需连接").first().waitFor();
    await page.getByRole("button", { name: /模型/ }).click();
    await page.getByText("模型提供方").waitFor();
    await page.getByRole("button", { name: "切换到此模型" }).first().waitFor();
    await page.getByTitle("配置模型连接").first().click();
    await page.getByRole("dialog").waitFor();
    await page.getByText("API Key").waitFor();
    await assertNoHorizontalOverflow(page, `${viewport.name}/model-settings`);
    await page.getByTitle("关闭").click();
    await assertNoHorizontalOverflow(page, `${viewport.name}/capabilities`);

    await clickVisibleButton(page, "对话");
    await page.getByRole("heading", { name: "Agent 对话" }).waitFor();
    await page.getByPlaceholder("输入消息").waitFor();
    await page.getByText("运行轨迹").waitFor();
    const draft = "页面切换后仍需保留";
    const chatInput = page.locator(".composer textarea");
    await chatInput.fill(draft);
    await clickVisibleButton(page, "活动");
    await clickVisibleButton(page, "对话");
    await chatInput.waitFor();
    if ((await chatInput.inputValue()) !== draft) {
      throw new Error(`${viewport.name}/chat: draft was lost after view switch`);
    }
    await assertNoHorizontalOverflow(page, `${viewport.name}/chat`);

    await clickVisibleButton(page, "活动");
    await page.getByRole("heading", { name: "工作区活动" }).waitFor();
    await page.getByText("谁在什么时候做了什么").waitFor();
    await assertNoHorizontalOverflow(page, `${viewport.name}/activity`);

    if (errors.length) {
      throw new Error(`${viewport.name}: browser errors:\n${errors.join("\n")}`);
    }

    results.push({
      viewport: viewport.name,
      errors,
      checks: "passed"
    });
    await context.close();
  }
} finally {
  await browser.close();
}

console.log(JSON.stringify(results, null, 2));

async function visibleButton(page, text) {
  const buttons = page.locator("button:visible").filter({ hasText: text });
  const count = await buttons.count();
  if (!count) {
    throw new Error(`Visible button not found: ${text}`);
  }
  return buttons.first();
}

async function clickVisibleButton(page, text) {
  const button = await visibleButton(page, text);
  await button.click();
}

async function assertNoHorizontalOverflow(page, label) {
  const metrics = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth
  }));
  const overflow = Math.max(metrics.document, metrics.body) - metrics.viewport;
  if (overflow > 1) {
    throw new Error(`${label}: horizontal overflow ${overflow}px (${JSON.stringify(metrics)})`);
  }
}
