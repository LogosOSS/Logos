#!/usr/bin/env node
/*
 * Captures the committed documentation screenshots at 2x pixel density
 * (1440×900 CSS px viewport, 2880 px wide PNGs) from the screenshots stack.
 * The shot list follows the shot matrix in docs/AGENTS.md.
 *
 * Prerequisites: the screenshots stack and `ng serve` on :4300 (docs/AGENTS.md,
 * "Stack"), the seed and the request workload example applied, and
 * `node docs/seed/nightly-worker.mjs` running.
 *
 *   cd docs
 *   NODE_PATH=../logos-ui/node_modules node seed/capture-screenshots.cjs
 *   NODE_PATH=../logos-ui/node_modules node seed/capture-screenshots.cjs roles/batches.png roles/team-detail-overview.png
 *
 * Arguments limit the run to the named files (paths relative to static/img/).
 * Without arguments, shots marked `needs` are left out: the seed does not
 * create their example state, and the committed PNG stays in place.
 * OUT_DIR writes the PNGs to another folder instead of overwriting static/img/.
 * The nightly shots change the schedule of llama-3.1-8b-instruct on Docs Local
 * Worker. Use only the isolated documentation stack.
 */
const { chromium } = require("playwright");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const BASE = process.env.LOGOS_UI_URL || "http://localhost:4300";
const DB_CONTAINER = process.env.LOGOS_DB_CONTAINER || "doks-db";
const IMG = path.join(__dirname, "..", "static", "img");
const OUT = process.env.OUT_DIR || IMG;
const ONLY = process.argv.slice(2);

const ADMIN = "tobias.wasner";
const APP_ADMIN = "alexandra.szuminska";
const DEVELOPER = "henriette.huhn";

function sql(query) {
  return execFileSync("docker", ["exec", DB_CONTAINER, "psql", "-U", "postgres", "-d", "logosdb", "-Atc", query]) // NOSONAR - developer tool: uses the docker CLI from the developer's PATH
    .toString()
    .trim();
}

function lookupIds() {
  const model = sql("SELECT id FROM models WHERE name = 'llama-3.1-8b-instruct'");
  const pair = sql(
    "SELECT mp.id FROM model_provider mp JOIN providers p ON p.id = mp.provider_id " +
      `WHERE p.name = 'Docs Local Worker' AND mp.model_id = ${model}`,
  );
  return {
    team: sql("SELECT id FROM teams WHERE name = 'Logos'"),
    model,
    // The second nightly night: its variant is the linked result in the guide.
    linkedBenchmark: sql(
      `SELECT id FROM model_provider_benchmarks WHERE model_provider_id = ${pair} ORDER BY recorded_at DESC OFFSET 2 LIMIT 1`,
    ),
  };
}

// ---------------------------------------------------------------- helpers

async function login(browser, user, dark = false) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    colorScheme: dark ? "dark" : "light",
    reducedMotion: "reduce",
  });
  await ctx.addInitScript((theme) => localStorage.setItem("logos-theme", theme), dark ? "dark" : "light");
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

// Marks the inbox read so that the bell shows no unread badge in the shots.
async function markNotificationsRead(page) {
  const bell = page.locator('button[aria-label*="otification"]').first();
  if (!(await bell.count())) return;
  await bell.click();
  const mark = page.getByText("Mark all as read").first();
  await mark.waitFor({ timeout: 5000 }).catch(() => {});
  if ((await mark.count()) && (await mark.isEnabled())) await mark.click();
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);
}

// Waits for fonts, for skeletons and spinners to disappear, then for paint.
async function settle(page, extra = 1500) {
  await page.evaluate(() => document.fonts.ready);
  await page
    .waitForFunction(
      () =>
        ![
          ...document.querySelectorAll(
            '[class*="skeleton"], app-stats-skeleton, .pi-spinner, .performance-message',
          ),
        ].some((e) => !e.closest(".benchmark-current-run") && e.getBoundingClientRect().height > 0),
      null,
      { timeout: 30000 },
    )
    .catch(() => console.log("  settle: loading indicator still visible"));
  await page.waitForTimeout(extra);
}

// Opens or closes the <details> whose own <summary> starts with `text`.
async function details(page, text, open = true) {
  const found = await page.evaluate(
    ({ text, open }) => {
      const hit = [...document.querySelectorAll("details > summary")].find(
        (s) => s.textContent.replace(/\s+/g, " ").trim().startsWith(text) && s.getClientRects().length,
      );
      if (!hit) return false;
      const d = hit.parentElement;
      if (d.open !== open) hit.click();
      return true;
    },
    { text, open },
  );
  if (!found) throw new Error(`details "${text}" not found`);
  await page.waitForTimeout(400);
}

const button = (page, name) => page.getByRole("button", { name, exact: true }).first();

// The shell scrolls inside .main-content; unlock it for a full-page shot and
// stretch fixed full-viewport layers (dialog backdrops) over the whole page.
async function unlock(page) {
  await page.addStyleTag({
    content: `html, body, .main-content { height: auto !important; max-height: none !important; overflow: visible !important; }
      .shell-layout { min-height: 100vh !important; height: auto !important; overflow: visible !important; align-items: stretch !important; }
      .sidebar { align-self: stretch !important; min-height: 100% !important; }`,
  });
  await page.evaluate(() => {
    const main = document.querySelector(".main-content");
    if (main) {
      main.querySelectorAll("*").forEach((n) => {
        const cs = getComputedStyle(n);
        const scrollsVertically = (cs.overflowY === "auto" || cs.overflowY === "scroll")
          && n.scrollHeight > n.clientHeight;
        if (scrollsVertically && n.scrollWidth <= n.clientWidth) {
          n.style.setProperty("overflow", "visible", "important");
          n.style.setProperty("height", "auto", "important");
          n.style.setProperty("max-height", "none", "important");
        }
      });
    }
  });
  await page.waitForTimeout(300);
  await page.evaluate(() => {
    const full = document.documentElement.scrollHeight;
    document.querySelectorAll("body *").forEach((n) => {
      const cs = getComputedStyle(n);
      const r = n.getBoundingClientRect();
      if (cs.position === "fixed" && r.top <= 0 && r.width >= innerWidth - 1 && r.height >= innerHeight - 1) {
        const children = [...n.children].map((c) => [c, c.getBoundingClientRect().top]);
        n.style.setProperty("bottom", "auto", "important");
        n.style.setProperty("height", full + "px", "important");
        for (const [c, top] of children) {
          const shift = top - c.getBoundingClientRect().top;
          if (Math.abs(shift) > 1) c.style.setProperty("translate", `0 ${shift}px`);
        }
      }
    });
  });
  await page.waitForTimeout(300);
}

// Sets the nightly schedule through the UI (button + confirmation dialog).
async function setNightly(page, enabled) {
  const toggle = page.getByRole("button", { name: /nightly runs…$/ }).first();
  await toggle.waitFor({ timeout: 30000 });
  await page.waitForFunction(
    () => ![...document.querySelectorAll(".nightly__status")].some((e) => e.textContent.includes("Loading")),
    null,
    { timeout: 30000 },
  );
  const label = (await toggle.textContent()).trim();
  if (label.startsWith("Disable") === enabled) return;
  await toggle.click();
  await button(page, enabled ? "Enable nightly runs" : "Disable nightly runs").click();
  await page.getByRole("button", { name: enabled ? "Disable nightly runs…" : "Enable nightly runs…" }).waitFor({
    timeout: 30000,
  });
  await page.waitForTimeout(1000);
}

async function performance(page, ids, view, query = "") {
  await page.goto(`${BASE}/models/${ids.model}/details?tab=performance${query}`);
  await settle(page);
  if (view) await button(page, view).click();
  await settle(page, 800);
}

// Run benchmark view with every disclosure closed; `open` lists those to open.
async function runView(page, ids, { nightly, open = [] }) {
  await performance(page, ids, "Run benchmark");
  await setNightly(page, nightly);
  for (const text of ["Request workload", "Schedule and test plan", "Initial parameter values", "Latest round results", "Dataset and settings", "Repeat or compare settings", "Glossary & abbreviations"]) {
    await details(page, text, open.includes(text)).catch(() => {});
  }
  await page.waitForTimeout(600);
}

async function resultsView(page, ids, sections = [], query = "") {
  await performance(page, ids, null, query);
  for (const section of sections) await details(page, section);
}

async function analysisView(page, ids, section) {
  await resultsView(page, ids, ["More analysis & details", section]);
}

async function selectSavedDataset(page) {
  const dataset = page.getByLabel("Input dataset", { exact: true });
  const value = await dataset.evaluate((select) => [...select.options].find((option) => option.value)?.value);
  if (!value) throw new Error("No saved request dataset is available");
  await dataset.selectOption(value);
  await settle(page, 600);
}

async function teamTab(page, ids, label) {
  await page.goto(`${BASE}/teams/${ids.team}`);
  await settle(page);
  await page.getByRole("tab", { name: label, exact: true }).click();
  await settle(page);
}

// AI Tools wizard up to the step titled `target` (Tool, Team, Model, Install,
// Connect, Verify). The Team step only appears for users with several keys.
async function aiTools(page, target, model) {
  await page.goto(`${BASE}/ai-tools`);
  await settle(page);
  if (target === "Tool") return;
  await page.getByRole("button", { name: "Choose Claude Code" }).click();
  await settle(page, 500);
  const current = () => page.locator('[aria-current="step"] .stepper-title').textContent().then((t) => t.trim());
  for (let i = 0; i < 6; i++) {
    let step = await current();
    if (step === "Tool") {
      await page.locator(".btn-primary", { hasText: "Continue" }).last().click();
      await settle(page, 600);
      step = await current();
    }
    if (step === "Model" && model) await page.locator('select[aria-label="Select model"]').selectOption(model);
    if (step === target) break;
    await page.locator(".btn-primary", { hasText: "Continue" }).last().click();
    await settle(page, 600);
  }
  if ((await current()) !== target) throw new Error(`AI Tools step "${target}" not reached`);
  await settle(page, 600);
}

// ---------------------------------------------------------------- shot list
// file: path relative to static/img/; full: full-page shot (else 1440×900).

const SHOTS = [
  // Logos Admin pages
  {
    file: "roles/logos-admin-benchmark-notification.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await resultsView(p, ids);
      await p.locator('button[aria-label*="otification"]').first().click();
      await p.getByRole("link", { name: /View benchmark #/ }).first().waitFor();
    },
    // Later shots should not show the unread badge.
    after: (p) => p.getByText("Mark all as read").first().evaluate((b) => b.click()),
  },

  { file: "roles/logos-admin-statistics.png", user: ADMIN, full: true, go: (p) => p.goto(BASE + "/statistics?tab=requests") },
  {
    file: "roles/logos-admin-statistics-requesters.png",
    user: ADMIN,
    go: async (p) => {
      await p.goto(BASE + "/statistics?tab=requests");
      await settle(p);
      await p.getByRole("button", { name: /All requesters/ }).first().click();
    },
  },
  { file: "roles/logos-admin-models.png", user: ADMIN, full: true, go: (p) => p.goto(BASE + "/models") },
  { file: "roles/logos-admin-providers.png", user: ADMIN, go: (p) => p.goto(BASE + "/providers") },
  {
    file: "roles/logos-admin-provider-connections.png",
    user: ADMIN,
    go: async (p) => {
      await p.goto(BASE + "/providers");
      await settle(p);
      await p.getByRole("button", { name: "Expand Docs Cloud (EU)" }).first().click();
      await p.locator('[data-testid="batch-support"]').first().waitFor();
    },
  },
  { file: "roles/logos-admin-policies.png", user: ADMIN, go: (p) => p.goto(BASE + "/policies") },
  { file: "roles/logos-admin-billing.png", user: ADMIN, go: (p) => p.goto(BASE + "/billing") },
  { file: "roles/logos-admin-user-management.png", user: ADMIN, go: (p) => p.goto(BASE + "/user-management") },
  { file: "roles/logos-admin-team-management.png", user: ADMIN, go: (p) => p.goto(BASE + "/team-management") },
  { file: "roles/logos-admin-permission-audit.png", user: ADMIN, full: true, go: (p, ids) => p.goto(`${BASE}/permission-audit-log?team_id=${ids.team}`) },
  {
    file: "roles/logos-admin-new-team.png",
    user: ADMIN,
    go: async (p) => {
      await p.goto(BASE + "/team-management");
      await settle(p);
      await p.getByRole("button", { name: /New Team/ }).click();
      await p.fill("#team-name", "MAISS");
      await p.fill("#team-keycloak-group", "maiss-dev");
      await p.locator("#team-keycloak-group").blur();
    },
  },
  { file: "roles/logos-admin-my-workspace.png", user: ADMIN, go: (p) => p.goto(BASE + "/my-workspace") },
  { file: "roles/team-detail-providers.png", user: ADMIN, go: (p, ids) => teamTab(p, ids, "Providers") },
  { file: "roles/team-detail-repositories.png", user: ADMIN, go: (p, ids) => teamTab(p, ids, "Repositories") },
  { file: "roles/team-detail-settings.png", user: ADMIN, full: true, go: (p, ids) => teamTab(p, ids, "Settings") },
  {
    file: "roles/team-detail-workflows.png",
    user: ADMIN,
    full: true,
    needs: "an owner-edited diagram with a differing agent update (Edited / Agent update / Keep mine)", go: (p, ids) => teamTab(p, ids, "Workflows") },

  {
    file: "roles/logos-admin-benchmark-window-ended.png",
    user: ADMIN,
    full: true,
    needs: "LOGOS_DOCS_WINDOW_ENDED=true request-workload-example.py seed",
    go: async (p, ids) => {
      await resultsView(p, ids);
      await p.getByRole("status", { name: "Benchmark stopped" }).waitFor();
    },
  },
  // Model performance: overview, results and analysis
  {
    file: "roles/logos-admin-nightly-results.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => resultsView(p, ids),
  },
  {
    file: "roles/logos-admin-benchmark-linked-result.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await resultsView(p, ids, [], `&benchmark=${ids.linkedBenchmark}`);
      await p.getByRole("region", { name: "Selected benchmark result" }).waitFor();
    },
  },
  {
    file: "roles/logos-admin-benchmark-comparison.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => analysisView(p, ids, "Charts and comparison"),
  },
  {
    file: "roles/logos-admin-benchmark-configuration.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await analysisView(p, ids, "Run details and saved configurations");
      await p.locator(".benchmark-configuration > summary").first().click();
    },
  },
  {
    file: "roles/logos-admin-benchmark-glossary.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => analysisView(p, ids, "Glossary & abbreviations"),
  },
  // Model performance: Run benchmark view and the nightly schedule
  {
    file: "roles/logos-admin-nightly-benchmarks.png",
    user: ADMIN,
    full: true,
    go: (p, ids) =>
      runView(p, ids, { nightly: true, open: ["Schedule and test plan"] }),
    after: (p) => setNightly(p, false),
  },
  {
    file: "roles/logos-admin-nightly-workload.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, { nightly: false, open: ["Request workload"] });
      await details(p, "Create a fixed dataset from FULL request logs");
    },
  },
  {
    file: "roles/logos-admin-nightly-enable.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, {
        nightly: false,
        open: ["Schedule and test plan"],
      });
      await p.getByRole("button", { name: "Enable nightly runs…" }).click();
      await p.waitForTimeout(800);
    },
  },
  {
    file: "roles/logos-admin-nightly-disable.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, {
        nightly: true,
        open: ["Schedule and test plan"],
      });
      await p.getByRole("button", { name: "Disable nightly runs…" }).click();
      await p.waitForTimeout(800);
    },
    after: async (p) => {
      await button(p, "Cancel").click();
      await setNightly(p, false);
    },
  },
  {
    file: "roles/logos-admin-benchmark-saved-requests.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, { nightly: false, open: ["Dataset and settings"] });
      await selectSavedDataset(p);
      await p.locator(".benchmark-settings-panel select").filter({ has: p.locator('option[value="concurrent"]') }).first().selectOption("concurrent");
      await p.getByLabel("Concurrent requests", { exact: true }).fill("4");
      await p.locator("body").click({ position: { x: 5, y: 890 } });
      await p.waitForTimeout(600);
    },
  },
  {
    file: "roles/logos-admin-benchmark-frozen-batch.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, { nightly: true, open: ["Dataset and settings"] });
      await selectSavedDataset(p);
      await details(p, "Dataset and settings", false);
      await details(p, "Repeat or compare settings");
      await p.getByLabel("Run a batch").check();
      await p.locator('input[aria-label="Repetitions per configuration"]').fill("1");
      const parameter = p.locator('select[aria-label="Add a parameter to vary"]');
      for (const [label, values] of [
        ["Max sequences (0 = auto)", "4, 8"],
        ["Max batched tokens", "2048, 4096"],
      ]) {
        await parameter.selectOption({ label });
        await p.locator('input[aria-label="Values separated by commas"]').last().fill(values);
      }
      await details(p, "View run plan & fixed settings");
    },
    after: (p) => setNightly(p, false),
  },
  {
    file: "roles/logos-admin-benchmark-confirmation.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, { nightly: false });
      await button(p, "Review benchmark").click();
    },
    after: (p) => button(p, "Cancel").evaluate((b) => b.click()),
  },
  {
    file: "roles/logos-admin-benchmark-worker-settings.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, { nightly: false, open: ["Dataset and settings"] });
      await details(p, "vLLM settings for the next run");
    },
  },
  {
    file: "roles/logos-admin-deployed-configuration.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => resultsView(p, ids, ["Configuration & selection details"]),
  },
  {
    file: "roles/logos-admin-model-request-measurements.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => resultsView(p, ids, ["Request measurements"]),
  },
  {
    file: "roles/logos-admin-benchmark-status.png",
    user: ADMIN,
    full: true,
    needs: "the illustrative running job from LOGOS_DOCS_ACTIVE_BENCHMARK=true",
    go: async (p, ids) => {
      await resultsView(p, ids);
      await p.locator(".benchmark-current-run").waitFor();
    },
  },
  {
    file: "roles/logos-admin-nightly-automatic-source.png",
    user: ADMIN,
    full: true,
    needs: "LOGOS_NIGHTLY_REQUEST_USERNAME in the documentation orchestrator",
    go: async (p, ids) => {
      await runView(p, ids, { nightly: true });
      await p.getByText(/Latest \d+ successful FULL requests/).waitFor();
    },
    after: (p) => setNightly(p, false),
  },

  // App Admin
  { file: "roles/app-admin-models.png", user: APP_ADMIN, go: (p) => p.goto(BASE + "/models") },
  { file: "roles/app-admin-user-management.png", user: APP_ADMIN, go: (p) => p.goto(BASE + "/user-management") },
  { file: "roles/app-admin-team-management.png", user: APP_ADMIN, go: (p) => p.goto(BASE + "/team-management") },
  { file: "roles/app-admin-my-workspace.png", user: APP_ADMIN, go: (p) => p.goto(BASE + "/my-workspace") },
  { file: "roles/batches.png", user: APP_ADMIN, full: true, go: (p) => p.goto(BASE + "/batches") },
  { file: "roles/team-detail-overview.png", user: APP_ADMIN, go: (p, ids) => teamTab(p, ids, "Overview") },
  { file: "roles/team-detail-members.png", user: APP_ADMIN, go: (p, ids) => teamTab(p, ids, "Members") },
  {
    file: "roles/team-detail-application-keys.png",
    user: APP_ADMIN,
    go: (p, ids) => teamTab(p, ids, "Application Keys"),
  },
  { file: "roles/team-detail-models.png", user: APP_ADMIN, go: (p, ids) => teamTab(p, ids, "Models") },
  { file: "roles/team-detail-activity.png", user: APP_ADMIN, full: true, go: (p, ids) => teamTab(p, ids, "Activity") },
  {
    file: "roles/team-detail-cloud-usage.png",
    user: APP_ADMIN,
    full: true,
    go: (p, ids) => teamTab(p, ids, "Cloud Usage"),
  },
  { file: "roles/ai-tools-step-tool.png", user: APP_ADMIN, go: (p) => aiTools(p, "Tool") },
  {
    file: "roles/ai-tools-step-team.png",
    user: APP_ADMIN,
    needs: "a user with more than one API key (the seed gives each user one)", go: (p) => aiTools(p, "Team") },
  { file: "roles/ai-tools-step-model.png", user: APP_ADMIN, go: (p) => aiTools(p, "Model", "llama-3.1-8b-instruct") },
  { file: "roles/ai-tools-step-install.png", user: APP_ADMIN, go: (p) => aiTools(p, "Install", "llama-3.1-8b-instruct") },

  // App Developer
  { file: "roles/app-developer-models.png", user: DEVELOPER, go: (p) => p.goto(BASE + "/models") },
  { file: "roles/app-developer-my-workspace.png", user: DEVELOPER, go: (p) => p.goto(BASE + "/my-workspace") },
  {
    file: "roles/ai-tools-step-connect.png",
    user: DEVELOPER,
    full: true,
    go: (p) => aiTools(p, "Connect", "mistral-small-3.2-24b"),
  },
  {
    file: "roles/ai-tools-step-verify.png",
    user: DEVELOPER,
    full: true,
    go: (p) => aiTools(p, "Verify", "mistral-small-3.2-24b"),
  },
];

// ---------------------------------------------------------------- main

(async () => {
  const ids = lookupIds();
  console.log("ids", ids);
  // Shots marked `needs` require example data that the seed does not create;
  // they run only when named on the command line.
  const selected = SHOTS.filter((s) => (ONLY.length ? ONLY.includes(s.file) : !s.needs));
  for (const s of SHOTS) if (!ONLY.length && s.needs) console.log("not selected", s.file, "- needs", s.needs);
  const browser = await chromium.launch();
  let session = null;
  const failed = [];
  for (const shot of selected) {
    const key = shot.user + (shot.dark ? ":dark" : "");
    if (!session || session.key !== key) {
      if (session) await session.ctx.close();
      session = { key, ...(await login(browser, shot.user, shot.dark)) };
      if (!shot.file.endsWith("benchmark-notification.png")) await markNotificationsRead(session.page);
    }
    const { page } = session;
    try {
      await shot.go(page, ids);
      await settle(page);
      if (shot.full) await unlock(page);
      const target = path.join(OUT, shot.file);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      await page.screenshot({ path: target, fullPage: !!shot.full });
      console.log("shot", shot.file);
      if (shot.after) {
        await shot.after(page, ids);
        await page.waitForTimeout(1000);
      }
    } catch (err) {
      // The previous file stays in place; the summary lists every skipped shot.
      failed.push(shot.file);
      console.log("SKIPPED", shot.file, "-", err.message.split("\n")[0]);
    }
  }
  if (session) await session.ctx.close();
  await browser.close();
  if (failed.length) {
    console.log("Not captured:", failed.join(", "));
    process.exitCode = 1;
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
