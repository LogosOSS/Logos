#!/usr/bin/env node
/*
 * Checks the generated SVG diagrams for layout defects and for text that is too
 * small in the documentation. For each SVG it reports:
 *   - text that overlaps other text,
 *   - text that a line or an arrow crosses,
 *   - text that sticks out of the box it starts in,
 *   - text smaller than MIN_PX when the diagram is shown at COLUMN_PX wide.
 *
 *   cd docs
 *   NODE_PATH=../logos-ui/node_modules node static/img/system-design/check-diagrams.cjs [file.svg ...]
 *
 * Exit code 1 if any defect is found.
 */
const path = require("node:path");
const fs = require("node:fs");
const { chromium } = require("playwright");

const COLUMN_PX = 823; // width of the docs content column at 1440 px (with table of contents)
const MIN_PX = 10; // smallest readable text size at that width

const dir = __dirname;
const files = process.argv.slice(2).length
  ? process.argv.slice(2).map((f) => path.resolve(f))
  : fs.readdirSync(dir).filter((f) => f.endsWith(".svg")).map((f) => path.join(dir, f));

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 2000, height: 1400 } });
  let failures = 0;
  for (const file of files) {
    await page.goto("file://" + file);
    const report = await page.evaluate(
      ({ COLUMN_PX, MIN_PX }) => {
        const svg = document.querySelector("svg");
        const vbW = svg.viewBox.baseVal.width || svg.getBoundingClientRect().width;
        const scale = COLUMN_PX / vbW;
        const r = (e) => e.getBoundingClientRect();
        const inter = (a, b, pad = 0) =>
          a.left + pad < b.right && b.left + pad < a.right && a.top + pad < b.bottom && b.top + pad < a.bottom;
        const contains = (o, i) => i.left >= o.left - 0.5 && i.right <= o.right + 0.5 && i.top >= o.top - 0.5 && i.bottom <= o.bottom + 0.5;
        const texts = [...svg.querySelectorAll("text")].filter((t) => t.textContent.trim()).map((t) => ({ t, b: r(t), s: t.textContent.trim() }));
        const issues = [];
        // Text size at the docs column width.
        const fontPx = (t) => parseFloat(getComputedStyle(t).fontSize) || 0;
        const small = [...new Set(texts.filter((x) => fontPx(x.t) * scale < MIN_PX).map((x) => fontPx(x.t)))];
        if (small.length) issues.push(`font-size ${small.join(", ")} renders below ${MIN_PX}px at ${COLUMN_PX}px (scale ${scale.toFixed(2)}); minimum ${Math.ceil((MIN_PX / scale) * 10) / 10}`);
        // Text on text.
        for (let i = 0; i < texts.length; i++)
          for (let j = i + 1; j < texts.length; j++)
            if (inter(texts[i].b, texts[j].b, 1)) issues.push(`text overlaps text: "${texts[i].s}" / "${texts[j].s}"`);
        // Lines through text: sample every stroked path and line.
        const strokes = [...svg.querySelectorAll("path, line, polyline")].filter((p) => {
          const cs = getComputedStyle(p);
          return cs.stroke !== "none" && cs.stroke !== "" && !p.closest("marker") && !p.closest("defs");
        });
        for (const p of strokes) {
          const len = p.getTotalLength ? p.getTotalLength() : 0;
          const ctm = p.getScreenCTM();
          const hit = new Set();
          for (let d = 0; d <= len; d += 2) {
            const pt = p.getPointAtLength(d).matrixTransform(ctm);
            for (const x of texts) {
              const b = x.b;
              if (pt.x > b.left + 1 && pt.x < b.right - 1 && pt.y > b.top + 2 && pt.y < b.bottom - 2) hit.add(x.s);
            }
          }
          for (const s of hit) issues.push(`line crosses text: "${s}"`);
        }
        // Text outside the drawing.
        const canvas = r(svg);
        for (const x of texts) if (!contains(canvas, x.b)) issues.push(`text outside the drawing: "${x.s}"`);
        // Text that a shape covers only in part: a box border, a frame, an icon or an interface ball
        // runs through the text. A shape that fully contains the text (its own box) is fine.
        const shapes = [...svg.querySelectorAll("rect, circle")]
          .filter((e) => !e.closest("defs") && !e.closest("marker"))
          .map((e) => ({ e, b: r(e) }))
          .filter(({ b }) => b.width < canvas.width * 0.98);
        for (const x of texts) {
          const inner = { left: x.b.left + 1, right: x.b.right - 1, top: x.b.top + 3, bottom: x.b.bottom - 3 };
          for (const { e, b } of shapes) {
            if (!inter(inner, b)) continue;
            if (contains(b, x.b)) continue; // the text sits inside this shape
            if (e.tagName === "rect" && e.getAttribute("fill") && e.getAttribute("fill") !== "none" && contains(x.b, b)) continue;
            issues.push(`shape cuts through text: "${x.s}" (${e.tagName})`);
            break;
          }
        }
        return { vbW, scale: scale.toFixed(2), issues: [...new Set(issues)] };
      },
      { COLUMN_PX, MIN_PX },
    );
    const name = path.relative(process.cwd(), file);
    if (report.issues.length) {
      failures += report.issues.length;
      console.log(`✗ ${name} (viewBox ${report.vbW}, scale ${report.scale})`);
      for (const i of report.issues) console.log(`    ${i}`);
    } else {
      console.log(`✓ ${name} (viewBox ${report.vbW}, scale ${report.scale})`);
    }
  }
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
