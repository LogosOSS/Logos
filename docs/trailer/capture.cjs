#!/usr/bin/env node
/*
 * Captures the 2x (2880 px wide) screenshots in trailer/shots/ from the
 * screenshots stack. See trailer/README.md for the full procedure.
 *
 *   cd docs
 *   NODE_PATH=../logos-ui/node_modules node trailer/capture.cjs login-only
 *   NODE_PATH=../logos-ui/node_modules node trailer/capture.cjs shots <teamId> <modelId>
 *
 * "login-only" signs in the three seeded users once so that the seed finds them.
 * "shots" signs in as tobias.wasner and writes every shot to trailer/shots/.
 */
const { chromium } = require("playwright");
const path = require("node:path");

const [MODE, TEAM_ID, MODEL_ID] = process.argv.slice(2);
const OUT = path.join(__dirname, "shots");
const BASE = process.env.LOGOS_UI_URL || "http://localhost:4300";

// [file name, route, full page, optional action before the shot]
const SHOTS = [
  ["policies", "/policies", false],
  ["models", "/models", true],
  ["statistics", "/statistics?tab=requests", true],
  ["billing", "/billing", false, (page) => page.getByText("Last 30 Days").first().click()],
  ["team-overview", () => `/teams/${TEAM_ID}`, false],
  ["ai-tools", "/ai-tools", false],
  ["performance", () => `/models/${MODEL_ID}/details?tab=performance`, false],
];

async function login(browser, user) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    colorScheme: "light",
  });
  await ctx.addInitScript(() => localStorage.setItem("logos-theme", "light"));
  const page = await ctx.newPage();
  await page.goto(BASE + "/");
  await page.getByText(/Sign in with TUM/i).first().click({ timeout: 30000 });
  await page.waitForSelector("#username", { timeout: 30000 });
  await page.fill("#username", user);
  await page.fill("#password", "password");
  await page.click("#kc-login");
  await page.waitForURL((u) => u.toString().startsWith(BASE), { timeout: 30000 });
  await page.waitForTimeout(3000);
  return { ctx, page };
}

// Waits for fonts and for skeleton placeholders to disappear.
async function settle(page) {
  await page.evaluate(() => document.fonts.ready);
  await page
    .waitForFunction(
      () =>
        ![...document.querySelectorAll('[class*="skeleton"], app-stats-skeleton')].some(
          (e) => e.getBoundingClientRect().height > 0,
        ),
      null,
      { timeout: 30000 },
    )
    .catch(() => console.log("  skeleton wait timed out"));
  await page.waitForTimeout(2500);
}

// The shell scrolls inside .main-content; unlock it for a full-page shot.
async function unlock(page) {
  await page.addStyleTag({
    content: `html, body, .main-content { height: auto !important; max-height: none !important; overflow: visible !important; }
      .shell-layout { min-height: 100vh !important; height: auto !important; overflow: visible !important; align-items: stretch !important; }
      .sidebar { align-self: stretch !important; min-height: 100% !important; }`,
  });
  await page.evaluate(() => {
    const main = document.querySelector(".main-content");
    if (!main) return;
    main.querySelectorAll("*").forEach((n) => {
      const cs = getComputedStyle(n);
      if (cs.overflowY === "auto" || cs.overflowY === "scroll") {
        n.style.setProperty("overflow", "visible", "important");
        n.style.setProperty("height", "auto", "important");
        n.style.setProperty("max-height", "none", "important");
      }
    });
  });
  await page.waitForTimeout(500);
}

(async () => {
  const browser = await chromium.launch();
  if (MODE === "login-only") {
    for (const user of ["tobias.wasner", "alexandra.szuminska", "henriette.huhn"]) {
      const { ctx, page } = await login(browser, user);
      console.log(user, "->", page.url());
      await ctx.close();
    }
  } else if (MODE === "shots" && TEAM_ID && MODEL_ID) {
    const { ctx, page } = await login(browser, "tobias.wasner");
    for (const [name, route, full, action] of SHOTS) {
      await page.goto(BASE + (typeof route === "function" ? route() : route));
      await settle(page);
      if (action) {
        await action(page);
        await settle(page);
      }
      if (full) await unlock(page);
      await page.screenshot({ path: path.join(OUT, name + ".png"), fullPage: full });
      console.log("shot", name);
    }
    await ctx.close();
  } else {
    console.error("usage: capture.cjs login-only | shots <teamId> <modelId>");
    process.exitCode = 2;
  }
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
