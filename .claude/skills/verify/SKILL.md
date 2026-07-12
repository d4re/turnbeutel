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

## Headless browser

Chromium is baked into the devcontainer image at `$PLAYWRIGHT_BROWSERS_PATH`
(/opt/pw-browsers). Install the matching client in the scratchpad and launch —
the version must match the image's pinned browser build:

```bash
cd <scratchpad> && npm i playwright-core@1.61.1
```

```js
const { chromium } = require("playwright-core");
const browser = await chromium.launch(); // finds the preinstalled browser
```

Leaflet and markercluster are served locally from `frontend/vendor/`, so no
request interception is needed for them. Only OSM tiles are firewalled:
fulfill `https://*.tile.openstreetmap.org/**` with a 1px PNG via `page.route`
(map background renders gray — markers/clusters still work).

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
- `frontend/node_modules` (eslint) is installed by the devcontainer
  postCreateCommand; if missing, `npm ci --prefix frontend`.
- The pre-commit hook runs backend tests + lint via `make`.
