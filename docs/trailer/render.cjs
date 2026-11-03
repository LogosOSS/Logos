#!/usr/bin/env node
/*
 * Renders trailer.html frame by frame and encodes static/video/logos-trailer.mp4
 * plus the poster image static/video/logos-trailer-poster.jpg.
 * Frames render at 2x device scale and are downscaled with Lanczos for sharp text.
 * The soundtrack comes from music.py (synthesized, no third-party audio).
 *
 * Requirements: ffmpeg and python3 with numpy on PATH, and the Playwright package with its Chromium build.
 * The docs site does not depend on Playwright; reuse the copy from logos-ui:
 *
 *   cd docs
 *   NODE_PATH=../logos-ui/node_modules node trailer/render.cjs [--fps 30] [--from 0] [--to 60]
 */
const { spawn, execFileSync } = require("node:child_process");
const os = require("node:os");
const path = require("node:path");
const fs = require("node:fs");
const { chromium } = require("playwright");

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []),
);
const FPS = Number(args.fps || 30);
const OUT_DIR = path.resolve(__dirname, "../static/video");
const OUT = path.join(OUT_DIR, args.out || "logos-trailer.mp4");
const POSTER = path.join(OUT_DIR, "logos-trailer-poster.jpg");
const POSTER_T = 4.6;

(async () => {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const music = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "logos-trailer-")), "music.wav");
  execFileSync("python3", ["-I", path.join(__dirname, "music.py"), music], { stdio: "inherit" });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 });
  await page.goto("file://" + path.join(__dirname, "trailer.html") + "?driven");
  await page.evaluate(() => window.trailerReady);
  const duration = await page.evaluate(() => window.DURATION);
  const from = Number(args.from || 0);
  const to = Math.min(Number(args.to || duration), duration);
  const frames = Math.round((to - from) * FPS);

  const ffmpeg = spawn(
    "ffmpeg",
    ["-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", String(FPS), "-i", "-",
      "-ss", String(from), "-t", String(to - from), "-i", music,
      "-vf", "scale=1920:1080:flags=lanczos", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
      "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
      "-movflags", "+faststart", OUT],
    { stdio: ["pipe", "inherit", "inherit"] },
  );
  const done = new Promise((resolve, reject) => ffmpeg.on("close", (c) => (c === 0 ? resolve() : reject(new Error("ffmpeg exit " + c)))));

  for (let i = 0; i < frames; i++) {
    await page.evaluate((t) => window.renderAt(t), from + i / FPS);
    const buf = await page.screenshot({ type: "jpeg", quality: 95 });
    if (!ffmpeg.stdin.write(buf)) await new Promise((r) => ffmpeg.stdin.once("drain", r));
    if (i % FPS === 0) process.stdout.write(`\r${(i / FPS).toFixed(0)} / ${(frames / FPS).toFixed(0)} s`);
  }
  ffmpeg.stdin.end();
  await done;

  if (!args.out) {
    const poster = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
    await poster.goto("file://" + path.join(__dirname, "trailer.html") + "?driven");
    await poster.evaluate(() => window.trailerReady);
    await poster.evaluate((t) => window.renderAt(t), POSTER_T);
    await poster.screenshot({ type: "jpeg", quality: 88, path: POSTER });
  }
  await browser.close();
  process.stdout.write(`\nwrote ${OUT}\n`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
