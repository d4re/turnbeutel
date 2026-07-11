---
name: verify
description: Build, launch, and drive Turnbeutel end-to-end in this sandbox (headless browser included) to verify frontend/backend changes at the real surface.
---

# Verifying Turnbeutel changes

## Launch the app

```bash
make serve   # background it; serves backend + frontend on :8000
# ready when: curl -s localhost:8000/api/cities returns JSON (starts in ~1s)
```

**Stop the server when verification is done** — don't leave it running in the
background after the task ends.

The USC upstream API is reachable from this sandbox, so live venue/course data
works. First venue fetch per city takes a few seconds; cached in
`backend/cache/usc.db` afterwards.

## Headless browser (sandbox has no browser and blocks the Playwright CDN)

Egress allowlist: npm registry and USC API work; `cdn.playwright.dev`,
`unpkg.com`, Debian mirrors, and OSM tiles are all BLOCKED. Recipe that works:

```bash
cd <scratchpad>
npm i playwright-core @sparticuz/chromium leaflet@1.9.4 leaflet.markercluster@1.5.3
# @sparticuz/chromium ships the browser inside the npm tarball, but its
# NSS libs only auto-extract on Amazon Linux. Extract them manually:
mkdir al2023 && cd al2023
node -e "const fs=require('fs'),z=require('zlib');fs.writeFileSync('al2023.tar',z.brotliDecompressSync(fs.readFileSync('../node_modules/@sparticuz/chromium/bin/al2023.tar.br')))"
tar xf al2023.tar   # yields lib/libnss3.so etc.
```

Launch from Node with:

```js
const { chromium: pw } = require("playwright-core");
const chromium = require("@sparticuz/chromium");
const browser = await pw.launch({
  executablePath: await chromium.executablePath(), // extracts to /tmp/chromium
  args: chromium.args,
});
```

and run the script with `LD_LIBRARY_PATH=<scratchpad>/al2023/lib node script.js`.

Because unpkg + OSM are blocked, intercept in Playwright: serve
`https://unpkg.com/leaflet@.../dist/*` and `leaflet.markercluster@.../dist/*`
from the locally installed npm packages via `page.route`, and fulfill
`https://*.tile.openstreetmap.org/**` with a 1px PNG (map background renders
gray — markers/clusters still work).

## Flows worth driving

- Mobile context: `{ viewport: {width:390,height:844}, hasTouch:true, isMobile:true }`.
  Check `#sidebar` `data-sheet` + `getBoundingClientRect().top` for snap states
  (peek ≈ H-110, half ≈ 0.5H, full ≈ 0.08H). Drag via `page.mouse` on
  `#sheet-handle`; tap list items; `#filters-button` overlay; `#filter-badge`.
- Desktop context (≥769px wide) must show the classic 380px sidebar, no handle.
- Popup-after-list-click needs ~2s before asserting `.leaflet-popup` (zoom
  animation + cluster release + retry loop).

## Gotchas

- `init()` must stay the last statement in `app.js` (TDZ on top-level consts).
- `frontend/` needs `npm install` once before `npx eslint app.js`.
- The pre-commit hook runs backend tests + lint via `make`.
