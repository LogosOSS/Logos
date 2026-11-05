# Homepage trailer

This folder holds the source of the 60 s trailer on the documentation
homepage. The output is `../static/video/logos-trailer.mp4` (1920×1080, H.264,
AAC) and the poster `../static/video/logos-trailer-poster.jpg`. The docs
plugin excludes this folder, so Docusaurus does not publish it as a page.

## Files

| File | Purpose |
|---|---|
| `trailer.html` | The trailer. Every frame is a pure function of the time `t`. Open the file in a browser for a live preview without sound. |
| `render.cjs` | Renders all frames with Playwright at 2x device scale, downscales them with Lanczos and encodes the MP4 with ffmpeg. It also writes the poster. |
| `music.py` | Synthesizes the soundtrack from sine, sawtooth and noise sources. The track contains no third-party audio. |
| `capture.cjs` | Captures the 2x screenshots in `shots/` from the screenshots stack. |
| `shots/` | 2x screenshots (2880 px wide) that the trailer shows. |

The lanes scene and the agents scene use the role-guide PNGs
`logos-admin-statistics-lanes.png` and `logos-admin-agents.png` from
`../static/img/roles/`. The screenshots stack has no GPU worker and no agent
runner, so `capture.cjs` cannot capture these 2 pages.

## Rules

- Use Helvetica for all text. The logo is the only exception.
- Keep text in the trailer short and in the product terms of the UI.
- Keep the scene times in `TIMELINE` and the cut times in `music.py`
  (`CUTS`, `OUTRO`) aligned.

## Re-render the video

Requirements: ffmpeg, python3 with numpy, and the Playwright package from
`logos-ui` with its Chromium build.

```bash
cd docs
NODE_PATH=../logos-ui/node_modules node trailer/render.cjs
```

A full render takes approximately 15 min. Use `--from` and `--to` (seconds)
and `--out` for a short test file, for example
`--from 6 --to 14 --out test.mp4`.

## Re-capture the screenshots

1. Start the screenshots stack and the web application on port 4300. The
   procedure is in `../AGENTS.md` (section "Stack").
2. Sign in the 3 seeded users once:

   ```bash
   cd docs
   NODE_PATH=../logos-ui/node_modules node trailer/capture.cjs login-only
   ```

3. Apply the seed:

   ```bash
   docker exec -i doks-db psql -U postgres -d logosdb < seed/role-screenshots.sql
   ```

4. Start the example worker in a second shell. If the worker is not
   connected, the **Nightly benchmark** column on **Models** shows
   **Blocked**.

   ```bash
   node seed/nightly-worker.mjs
   ```

5. Get the team id of **Logos** and the model id of `llama-3.1-8b-instruct`:

   ```bash
   docker exec doks-db psql -U postgres -d logosdb -Atc \
     "select id from teams where name = 'Logos'; select id from models where name = 'llama-3.1-8b-instruct';"
   ```

6. Capture the shots:

   ```bash
   NODE_PATH=../logos-ui/node_modules node trailer/capture.cjs shots <teamId> <modelId>
   ```

7. Stop the example worker with Ctrl+C and stop the stack.
8. Re-render the video.

If a shot changes its height, update `h` (the CSS height, half of the PNG
height) in `FEATURES` in `trailer.html`. Check the zoom regions (`focus`) and
the highlight rings (`rings`) in a preview. Their coordinates are in CSS
pixels of the 1440 px wide page.
