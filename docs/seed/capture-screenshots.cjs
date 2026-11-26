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
 * The Deployments shots need seed/deployments.sql after the role seed. The shot of the running deployment adds
 * seed/deployments-running.sql for that one shot and removes the deployment again.
 *
 * Arguments limit the run to the named files (paths relative to static/img/).
 * Without arguments, shots marked `needs` are left out: the seed does not
 * create their example state, and the committed PNG stays in place.
 * OUT_DIR writes the PNGs to another folder instead of overwriting static/img/.
 * CHROMIUM_PATH names a browser to use when Playwright does not find its own build.
 * LOGOS_SCREENSHOT_WIDTH overrides the viewport width for mobile PR screenshots.
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
const WIDTH = Number(process.env.LOGOS_SCREENSHOT_WIDTH || 1440);

// The screenshots stack does not run the agent runner; /api/agent must reach one
// that reads the documentation database.
const AGENT_API = "the agent runner API on /api/agent";

const ADMIN = "tobias.wasner";
const APP_ADMIN = "alexandra.szuminska";
const DEVELOPER = "henriette.huhn";
const NIGHTLY_SCHEDULE = "**/model_benchmarks/nightly/{status,configure}";

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
    worker: sql("SELECT id FROM providers WHERE name = 'Docs Local Worker'"),
    model,
    // Use an earlier variant to show its changed parameters.
    linkedBenchmark: sql(
      `SELECT id FROM model_provider_benchmarks WHERE model_provider_id = ${pair}
        AND configuration->'nightly' ? 'parameters' ORDER BY recorded_at DESC, id DESC OFFSET 1 LIMIT 1`,
    ),
  };
}

// ---------------------------------------------------------------- helpers

async function login(browser, user, dark = false) {
  const ctx = await browser.newContext({
    viewport: { width: WIDTH, height: 900 },
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
  await page.locator(".shell-layout").waitFor();
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
  await page.locator(".shell-layout").waitFor();
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

// The Providers page with the documentation worker expanded to its node models.
async function expandWorker(page) {
  await page.goto(BASE + "/providers");
  await settle(page);
  await page.getByRole("button", { name: "Expand Docs Local Worker" }).first().click();
  await page.locator("app-node-models .table-row").first().waitFor();
}

// The page of one model of the documentation worker (view mode).
async function nodeModel(page, ids, model) {
  await page.goto(`${BASE}/providers/${ids.worker}/models?model=${encodeURIComponent(model)}`);
  await page.locator("h1.page-title").waitFor();
}

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
  await page.evaluate(() => {
    for (const dialog of document.querySelectorAll(".tumaet-ui-dialog")) {
      const preview = dialog.querySelector(".import-preview-scroll");
      if (preview) {
        preview.style.setProperty("max-height", "none", "important");
        dialog.style.setProperty("height", "auto", "important");
        dialog.style.setProperty("overflow", "visible", "important");
        dialog.closest(".cdk-overlay-pane")?.style.setProperty("max-height", "none", "important");
      }
      if (!dialog.getClientRects().length) continue;
      dialog.style.setProperty("max-height", "none", "important");
      const content = dialog.querySelector(".tumaet-ui-dialog-content");
      content?.style.setProperty("overflow-y", "visible", "important");
      const height = dialog.getBoundingClientRect().height;
      if (height > innerHeight - 48) {
        const wrapper = dialog.closest(".cdk-global-overlay-wrapper");
        wrapper?.style.setProperty("align-items", "flex-start", "important");
        wrapper?.style.setProperty("padding-top", "24px", "important");
        wrapper?.style.setProperty("box-sizing", "border-box", "important");
        const full = Math.max(document.documentElement.scrollHeight, Math.ceil(height) + 48);
        document.querySelector(".shell-layout")?.style.setProperty("min-height", full + "px", "important");
      }
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

async function setNightly(page, enabled) {
  const customization = page.getByRole("dialog", { name: "Customize benchmark", exact: true });
  if (await customization.isVisible()) {
    await button(page, "Done").evaluate(b => b.click());
    await customization.waitFor({ state: "hidden" });
  }
  await page.getByRole("navigation", { name: "Performance sections" }).getByRole("button", { name: "Overview", exact: true }).click();
  const rotation = page.getByRole("region", { name: "Automatic benchmarks", exact: true });
  const tile = rotation.getByRole("button", { name: /^Docs Local Worker (Enabled|Disabled)\b/ });
  await tile.waitFor();
  if (await tile.getAttribute("aria-expanded") === "false") await tile.click();
  const worker = rotation.getByRole("region", { name: "Docs Local Worker", exact: true });
  if ((await tile.textContent()).includes("Enabled") === enabled) return;
  if (enabled) {
    await button(worker, "Enable automatic benchmarks").click();
  } else {
    await openNightlyDisable(page);
    const dialog = page.getByRole("dialog", { name: "Disable automatic benchmarks?", exact: true });
    await button(dialog, "Disable automatic benchmarks").evaluate(b => b.click());
  }
  await rotation.getByRole("button", { name: enabled ? /^Docs Local Worker Enabled\b/ : /^Docs Local Worker Disabled\b/ }).waitFor();
  await page.waitForTimeout(600);
}

async function openNightlyDisable(page) {
  await button(page, "Edit schedule").click();
  const editor = page.getByRole("dialog", { name: "Edit schedule", exact: true });
  await button(editor, "Disable automatic benchmarks").click();
}

async function performance(page, ids, view, query = "") {
  await page.goto(`${BASE}/models/${ids.model}/details?tab=performance${query}`);
  await settle(page);
  if (view) await button(page, view).click();
  await settle(page, 800);
}

async function nightlyView(page, ids, nightly) {
  await performance(page, ids, null);
  await setNightly(page, nightly);
}

async function runView(page, ids, customize = false) {
  await performance(page, ids, "Benchmarks");
  if (customize) await button(page, "Customize").click();
}

async function resultsView(page, ids, sections = [], query = "") {
  await performance(page, ids, null, query);
  for (const section of sections) {
    if (section === "Request measurements") await page.getByRole("navigation", { name: "Performance sections" }).getByRole("button", { name: section, exact: true }).click();
    else await details(page, section);
  }
}

async function analysisView(page, ids, section) {
  await performance(page, ids, "Analysis");
  await details(page, section);
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

// The Deployments page. seed/deployments.sql makes two deployers and the finished deployments #139 to #142.
// Logos counts a deployer as online for 90 s after its last request, so every shot marks the example deployers as
// seen first. The running deployment (#143) exists for one shot only: it switches the button Deploy off.
const RUNNING_DEPLOYMENT = fs.readFileSync(path.join(__dirname, "deployments-running.sql"), "utf8");

function psql(input) {
  execFileSync("docker", ["exec", "-i", DB_CONTAINER, "psql", "-U", "postgres", "-d", "logosdb", "-q", "-v", "ON_ERROR_STOP=1"], { input }); // NOSONAR - developer tool: uses the docker CLI from the developer's PATH
}

function prepareDeployments() {
  sql("DELETE FROM deployment_plans WHERE number = 143");
  sql("DELETE FROM deployers WHERE name = 'Docs standby host'");
  sql("UPDATE deployers SET last_seen_at = now() WHERE name IN ('Docs core host', 'Docs Local Worker')");
}

async function deploymentsPage(page) {
  prepareDeployments();
  await page.goto(BASE + "/deployments");
  await page.locator("table.deployers").waitFor();
  await page.locator("table.deployers .lg-dot--succeeded").nth(1).waitFor();
}

// The page of one finished deployment, by its number on this Logos.
async function deploymentPage(page, number) {
  prepareDeployments();
  const id = sql(`SELECT id FROM deployment_plans WHERE number = ${number}`);
  await page.goto(`${BASE}/deployments/${id}`);
  await page.locator("ol.timeline").first().waitFor();
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

  {
    file: "roles/logos-admin-statistics.png",
    user: ADMIN,
    full: true,
    go: async (p) => {
      await p.goto(BASE + "/statistics?tab=requests");
      await p.locator(".rr-badge--benchmark").waitFor();
    },
  },
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
  {
    file: "roles/model-requests.png",
    user: ADMIN,
    full: true,
    go: async (p) => {
      await p.goto(BASE + "/models?tab=requests");
      await p.getByRole("button", { name: /^License agreement for logos-docs\/example-chat/ }).waitFor();
    },
  },
  {
    file: "roles/model-request-license.png",
    user: ADMIN,
    go: async (p) => {
      await p.goto(BASE + "/models?tab=requests");
      await p.getByRole("button", { name: /^License agreement for logos-docs\/example-chat/ }).click();
      await p.getByText("Example use policy for the documentation model.", { exact: true }).waitFor();
    },
    after: (p) => button(p, "Cancel").click(),
  },
  {
    file: "roles/model-request-gated.png",
    user: ADMIN,
    go: async (p) => {
      await p.goto(BASE + "/models?tab=requests");
      await p.getByRole("button", { name: /example-chat repository is gated/ }).click();
      await button(p, "Go to Hugging Face").waitFor();
    },
    after: (p) => button(p, "Cancel").click(),
  },
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
  {
    file: "roles/logos-admin-edit-cloud-key.png",
    user: ADMIN,
    full: true,
    go: async (p) => {
      await p.goto(BASE + "/providers");
      await settle(p);
      await button(p, "Edit provider Docs Cloud (EU)").click();
      await p.getByText(/I verified EU processing/).waitFor();
    },
    after: (p) => p.keyboard.press("Escape"),
  },
  {
    file: "roles/logos-admin-add-provider.png",
    full: true,
    user: ADMIN,
    go: async (p) => {
      await p.goto(BASE + "/providers");
      await settle(p);
      await button(p, "Add Provider").click();
      await p.locator("select#add-cloud-type").selectOption("anthropic");
      await p.locator("#add-name").fill("Anthropic");
    },
    after: (p) => p.keyboard.press("Escape"),
  },
  {
    file: "roles/logos-admin-node-models.png",
    user: ADMIN,
    go: expandWorker,
  },
  {
    file: "roles/logos-admin-node-model-view.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => nodeModel(p, ids, "llama-3.1-8b-instruct"),
  },
  {
    file: "roles/logos-admin-node-model-edit.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await nodeModel(p, ids, "llama-3.1-8b-instruct");
      await button(p, "Edit configuration").click();
      await p.locator(".savebar").waitFor();
      // The save bar sticks to the window; in a full-page shot it belongs at the end.
      await p.addStyleTag({ content: ".savebar { position: static !important; }" });
    },
  },
  {
    file: "roles/logos-admin-node-model-calibrate.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await nodeModel(p, ids, "mistral-small-3.2-24b");
      await p.getByRole("button", { name: "Calibrate model" }).waitFor();
    },
  },
  {
    file: "roles/logos-admin-node-model-edit-settings.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await nodeModel(p, ids, "mistral-small-3.2-24b");
      await button(p, "Edit settings").click();
      await p.locator(".grid--single .stack").first().waitFor();
      await p.addStyleTag({ content: ".savebar { position: static !important; }" });
    },
  },
  {
    file: "roles/logos-admin-node-model-unsupported.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      const model = "mistral-small-3.2-24b";
      // The documentation worker has no failed calibration. Show the reported failure for this catalog model.
      await p.route("**/api/logosdb/node_config/insights", async (route) => {
        const response = await route.fetch();
        const insights = await response.json();
        const entry = insights.models.find((m) => m.model_name === model);
        entry.calibration = {
          ...entry.calibration,
          status: "unsupported",
          reason: "The model does not fit in the available GPU memory.",
        };
        await route.fulfill({ response, json: insights });
      });
      await nodeModel(p, ids, model);
      await p.getByRole("link", { name: "Open the error report" }).waitFor();
    },
    after: (p) => p.unroute("**/api/logosdb/node_config/insights"),
  },
  {
    file: "roles/logos-admin-node-connect-model.png",
    user: ADMIN,
    go: async (p, ids) => {
      // The documentation catalog lists only the three models the worker already has.
      // Add a fourth to the catalog answer, so the dialog shows a choice.
      await p.route("**/api/logosdb/get_models", async (route) => {
        const response = await route.fetch();
        const models = await response.json();
        await route.fulfill({ response, json: [...models, { ...models[0], id: 9999, name: "gemma-3-27b-it" }] });
      });
      await expandWorker(p);
      await button(p, "Connect model").click();
      await p.getByText("Add a model from the Logos catalog to this node.").waitFor();
      await p.getByRole("option", { name: "Select model…" }).waitFor({ state: "attached" });
    },
    after: async (p) => {
      await button(p, "Cancel").evaluate((b) => b.click());
      await p.unroute("**/api/logosdb/get_models");
    },
  },
  {
    file: "roles/logos-admin-node-settings.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await p.goto(`${BASE}/providers/${ids.worker}/node-settings`);
      await p.locator(".seg").waitFor();
      await p.addStyleTag({ content: ".savebar { position: static !important; }" });
    },
  },
  {
    file: "roles/logos-admin-deployments.png",
    user: ADMIN,
    full: true,
    go: async (p) => {
      await deploymentsPage(p);
      await p.fill("#deploy-tag", "1.0.3");
    },
  },
  {
    file: "roles/logos-admin-deployments-running.png",
    user: ADMIN,
    go: async (p) => {
      prepareDeployments();
      psql(RUNNING_DEPLOYMENT);
      await p.goto(BASE + "/deployments");
      await p.locator(".node").nth(1).waitFor();
    },
    after: () => sql("DELETE FROM deployment_plans WHERE number = 143"),
  },
  {
    file: "roles/logos-admin-deployments-confirm.png",
    user: ADMIN,
    go: async (p) => {
      await deploymentsPage(p);
      await p.fill("#deploy-tag", "1.0.3");
      await button(p, "Deploy").click();
      await p.getByText("Deploy 1.0.3?").waitFor();
    },
    // Never confirm: the dialog would start a deployment.
    after: (p) => button(p, "Cancel").evaluate((b) => b.click()),
  },
  { file: "roles/logos-admin-deployment-detail.png", user: ADMIN, full: true, go: (p) => deploymentPage(p, 142) },
  { file: "roles/logos-admin-deployment-rolled-back.png", user: ADMIN, full: true, go: (p) => deploymentPage(p, 140) },
  {
    file: "roles/logos-admin-deployer-key.png",
    user: ADMIN,
    go: async (p) => {
      await deploymentsPage(p);
      await button(p, "Add a core deployer").click();
      await p.fill("#deployer-name", "Docs standby host");
      await button(p, "Make the deployer").click();
      await p.locator('[data-testid="deployer-key"]').waitFor();
    },
    // The key is made up for the shot: the deployer goes away again.
    after: async (p) => {
      await button(p, "I have copied the key").evaluate((b) => b.click());
      sql("DELETE FROM deployers WHERE name = 'Docs standby host'");
    },
  },
  { file: "roles/logos-admin-billing.png", user: ADMIN, go: (p) => p.goto(BASE + "/billing") },
  { file: "roles/logos-admin-user-management.png", full: true, user: ADMIN, go: (p) => p.goto(BASE + "/user-management") },
  { file: "roles/logos-admin-team-management.png", full: true, user: ADMIN, go: (p) => p.goto(BASE + "/team-management") },
  { file: "roles/logos-admin-permission-audit.png", user: ADMIN, full: true, go: (p, ids) => p.goto(`${BASE}/permission-audit-log?team_id=${ids.team}`) },
  {
    file: "roles/logos-admin-new-team.png",
    full: true,
    user: ADMIN,
    go: async (p) => {
      await p.goto(BASE + "/team-management");
      await settle(p);
      await p.getByRole("button", { name: /New Team/ }).click();
      await p.fill("#team-name", "MAISS");
      await p.locator("#team-privacy").selectOption("EU");
      await p.getByRole("textbox", { name: "Search owners…" }).fill("szum");
      await p.getByRole("checkbox", { name: "Alexandra Szuminska" }).check();
    },
  },
  {
    file: "roles/logos-admin-agents.png",
    user: ADMIN,
    full: true,
    needs: AGENT_API,
    go: async (p) => {
      await p.goto(BASE + "/agents");
      await p.getByText("Add input validation to the signup form").waitFor();
    },
  },
  {
    file: "roles/logos-admin-agents-messages.png",
    user: ADMIN,
    full: true,
    needs: AGENT_API,
    go: async (p) => {
      await p.goto(BASE + "/agents");
      const session = p.locator("li.session").filter({ hasText: "Fix the flaky login spec" });
      await session.locator(".session__head").click();
      await session.locator(".agent-message code").waitFor();
    },
  },
  {
    file: "roles/logos-admin-agents-new-session.png",
    user: ADMIN,
    full: true,
    needs: AGENT_API,
    go: async (p) => {
      await p.goto(BASE + "/agents");
      await settle(p);
      await button(p, "New session").click();
      await p.getByRole("combobox", { name: "Repository", exact: true }).selectOption({ label: "docs-example/sample-app (develop)" });
      await p.getByRole("combobox", { name: "Workspace", exact: true }).selectOption({ label: "sample-app" });
      await p.locator(".form-card textarea").first().fill("Fix the date format in the export of the order list");
      await p.locator(".form-card textarea").first().blur();
      await p.getByText("Deploy to dev and screenshots are available only for the platform repository.").waitFor();
    },
  },
  { file: "roles/logos-admin-my-workspace.png", user: ADMIN, go: (p) => p.goto(BASE + "/my-workspace") },
  { file: "roles/team-detail-providers.png", full: true, user: ADMIN, go: (p, ids) => teamTab(p, ids, "Providers") },
  { file: "roles/team-detail-repositories.png", user: ADMIN, full: true, go: (p, ids) => teamTab(p, ids, "Repositories") },
  { file: "roles/team-detail-repository-agent.png", user: ADMIN, full: true, go: async (p, ids) => {
    await teamTab(p, ids, "Repositories");
    await p.locator('.table-row').filter({ hasText: 'docs-example/sample-app' }).getByRole('button', { name: 'Edit repository link' }).click();
    await p.locator('.tumaet-ui-dialog-title').filter({ hasText: 'Edit repository link' }).waitFor();
  } },
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
      await p.getByRole("dialog", { name: `Benchmark #${ids.linkedBenchmark}`, exact: true }).waitFor();
    },
  },
  {
    file: "roles/logos-admin-benchmark-comparison.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => analysisView(p, ids, "Compare parameter values"),
  },
  {
    file: "roles/logos-admin-benchmark-configuration.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await analysisView(p, ids, "Inspect saved runs");
      await p.locator(".benchmark-configuration > summary").first().click();
    },
  },
  {
    file: "roles/logos-admin-benchmark-glossary.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => analysisView(p, ids, "Benchmark glossary"),
  },
  // Model performance: Automatic benchmarks and manual setup
  {
    file: "roles/logos-admin-nightly-benchmarks.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => nightlyView(p, ids, true),
    after: (p) => setNightly(p, false),
  },
  {
    file: "roles/logos-admin-nightly-workload.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await nightlyView(p, ids, false);
      await p.getByRole("combobox", { name: "Request workload", exact: true }).first().selectOption("recent");
    },
  },
  {
    file: "roles/logos-admin-nightly-enable.png",
    user: ADMIN,
    full: true,
    go: (p, ids) => nightlyView(p, ids, false),
  },
  {
    file: "roles/logos-admin-nightly-edit.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await nightlyView(p, ids, true);
      await button(p, "Edit schedule").click();
      await p.getByRole("dialog", { name: "Edit schedule", exact: true }).waitFor();
    },
    after: async (p) => {
      await button(p, "Cancel").click();
      await setNightly(p, false);
    },
  },
  {
    file: "roles/logos-admin-nightly-disable.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await nightlyView(p, ids, true);
      await openNightlyDisable(p);
      await p.waitForTimeout(800);
    },
    after: async (p) => {
      await button(p, "Cancel").evaluate(b => b.click());
      await setNightly(p, false);
    },
  },
  {
    file: "roles/logos-admin-benchmark-saved-requests.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, true);
      await selectSavedDataset(p);
      await p.locator(".benchmark-customization select").filter({ has: p.locator('option[value="concurrent"]') }).first().selectOption("concurrent");
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
      await runView(p, ids, true);
      await selectSavedDataset(p);
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
  },
  {
    file: "roles/logos-admin-benchmark-confirmation.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids);
      await button(p, "Review benchmark").click();
    },
    after: (p) => button(p, "Cancel").evaluate((b) => b.click()),
  },
  {
    file: "roles/logos-admin-benchmark-worker-settings.png",
    user: ADMIN,
    full: true,
    go: async (p, ids) => {
      await runView(p, ids, true);
      await p.getByText("vLLM settings for the next run", { exact: true }).click();
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
    go: async (p, ids) => {
      const endpoint = "**/api/logosdb/request_latency_stats";
      await p.route(endpoint, async (route) => {
        const range = route.request().postDataJSON();
        await route.fulfill({ json: {
          range: { start: range.start_date, end: range.end_date },
          rows: [
            { reasoningEffort: "low", requestCount: 60, ttftSamples: 60, upstreamP50Ms: 850, upstreamP95Ms: 1500 },
            { reasoningEffort: "medium", requestCount: 42, ttftSamples: 38, upstreamP50Ms: 1100, upstreamP95Ms: 2400 },
          ].map(row => ({
            modelId: Number(ids.model), modelName: "llama-3.1-8b-instruct",
            providerId: Number(ids.worker), providerName: "Docs Local Worker", local: true, errorCount: 0,
            queueP50Ms: 17, queueP95Ms: 55, ttftP50Ms: 135, ttftP95Ms: 420, ...row,
          })),
        } });
      });
      await resultsView(p, ids, ["Request measurements"]);
      await p.locator(".latency-chart").first().waitFor();
      await p.unroute(endpoint);
    },
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
    go: async (p, ids) => {
      await p.route(NIGHTLY_SCHEDULE, async (route) => {
        const response = await route.fetch();
        if (!response.ok()) return route.fulfill({ response });
        const status = await response.json();
        await route.fulfill({ response, json: {
          ...status,
          workload_policy: { kind: "recent_user", username: "docs.operator", request_count: 50, ready: true },
        } });
      });
      await nightlyView(p, ids, true);
      await p.getByText(/Latest \d+ successful FULL requests/).waitFor();
    },
    after: async (p) => {
      await p.unroute(NIGHTLY_SCHEDULE);
      await setNightly(p, false);
    },
  },

  // App Admin
  { file: "roles/app-admin-models.png", user: APP_ADMIN, go: (p) => p.goto(BASE + "/models") },
  { file: "roles/app-admin-user-management.png", full: true, user: APP_ADMIN, go: (p) => p.goto(BASE + "/user-management") },
  { file: "roles/app-admin-team-management.png", full: true, user: APP_ADMIN, go: (p) => p.goto(BASE + "/team-management") },
  { file: "roles/app-admin-my-workspace.png", user: APP_ADMIN, go: (p) => p.goto(BASE + "/my-workspace") },
  { file: "roles/batches.png", user: APP_ADMIN, full: true, go: (p) => p.goto(BASE + "/batches") },
  { file: "roles/team-detail-overview.png", user: APP_ADMIN, go: (p, ids) => teamTab(p, ids, "Overview") },
  {
    file: "roles/team-members-csv.png",
    user: APP_ADMIN,
    full: true,
    go: async (p, ids) => {
      await teamTab(p, ids, "Members");
      await p.getByRole("link", { name: /Import CSV/i }).click();
      const [chooser] = await Promise.all([
        p.waitForEvent("filechooser"),
        button(p, "Choose File").click(),
      ]);
      await chooser.setFiles({
        name: "members.csv", mimeType: "text/csv",
        buffer: Buffer.from("prename,name,email\nAlex,River,alex.river@example.org\nHenriette,Huhn,henriette.huhn@test.de"),
      });
      await p.getByText("alex.river@example.org", { exact: true }).waitFor();
    },
  },
  { file: "roles/team-detail-members.png", full: true, user: APP_ADMIN, go: (p, ids) => teamTab(p, ids, "Members") },
  {
    file: "roles/team-detail-application-keys.png",
    user: APP_ADMIN,
    go: (p, ids) => teamTab(p, ids, "Application Keys"),
  },
  { file: "roles/team-detail-models.png", user: APP_ADMIN, go: (p, ids) => teamTab(p, ids, "Models") },
  { file: "roles/team-detail-activity.png", user: APP_ADMIN, full: true, go: async (p, ids) => {
    await teamTab(p, ids, "Activity");
    await p.getByRole("button", { name: /^Filter by requester/ }).click();
    await p.getByRole("searchbox", { name: "Search requesters" }).fill("wasn");
    await p.getByRole("button", { name: /Tobias Wasner/i }).click();
  } },
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
    file: "roles/my-workspace-logging-choice.png",
    user: DEVELOPER,
    go: async (p) => {
      await p.goto(BASE + "/my-workspace");
      const team = p.locator(".team-card").filter({ has: p.getByText("Logos", { exact: true }) });
      await team.getByRole("button", { name: "Expand models", exact: true }).click();
      await p.getByRole("heading", { name: "Choose your logging level", exact: true }).waitFor();
    },
  },
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
  const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
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
