# Sprint 1 — Chrome Extension Foundation

**Status:** complete (pending human load-test sign-off) · **Date:** 2026-09-24

## What This Sprint Delivers

The AI intelligence layer physically exists inside YouTube: install the
extension → open YouTube → ✦ AI FAB → transparent futuristic panel →
close/minimize/restore → navigate to another video → the extension recognizes
the new video. **Zero fake sentiment data. Zero AI analysis. Zero backend.**

## Development Setup

```bash
npm install          # once
npm run dev          # vite build --watch  (rebuilds dist/ on change)
npm run build        # production build → dist/
npm run typecheck    # tsc --noEmit (strict)
npm test             # vitest (32 tests)
npm run verify       # typecheck + test + build (run before every hand-off)
```

## Loading the Extension in Chrome (acceptance procedure)

1. `npm run build`  → produces `dist/manifest.json` + `dist/content.js`.
2. Open Chrome → navigate to `chrome://extensions`.
3. Enable **Developer mode** (top-right toggle).
4. Click **Load unpacked** → select this project's **`dist/`** folder.
5. Open/refresh YouTube — the ✦ AI FAB appears bottom-right.
6. After code changes: `npm run build`, then press the reload icon on the card
   at `chrome://extensions`, then reload the YouTube tab.

Expected: no manifest errors/warnings on load; console shows
`[sentiment-ai] extension initialized / YouTube detected / video detected /
overlay mounted` on YouTube tabs.

## Architecture (summary — details in docs/architecture/)

```text
Manifest V3 (zero permissions, youtube-only content script, document_idle)
   └── content script (src/content/index.ts)
         ├── isYouTubeUrl guard
         ├── getCurrentVideoContext()  → store.setVideoContext()
         ├── observeNavigation()       → store.setVideoContext() on SPA change
         └── host #sentiment-ai-extension-root (inline styles, z-index ceiling,
              pointer-events:none) ── open Shadow DOM
               └── React app (createRoot)
                     ├── store via useSyncExternalStore
                     ├── FAB (closed) / Overlay (open|…|minimized)
                     └── AnalysisService seam (stub: honest "unavailable")
```

## Test Matrix & Results

### Automated (all passing — `npm run verify`)

| Suite | Tests | Covers |
|---|---|---|
| `detection.test.ts` | 7 | YouTube host allow-list, look-alike domains, non-https, garbage input |
| `video-context.test.ts` | 12 | watch id extraction/validation, extra params, shorts/live, home/search/channel, non-YouTube, malformed URLs |
| `store.test.ts` | 6 | closed start, open→ready, minimize/restore preserving status, close resets, future states (analyzing/complete/error), video A→B update + subscriber notifications |
| `App.test.tsx` (jsdom) | 7 | FAB opens dialog + READY + video id, close returns FAB, minimize keeps state (notice probe) & restore, Escape close, honest analyze flow (notice, no fake numbers), unsupported-page state, video-detection-failure state |
| **Total** | **32** | |

### Build checks

| Check | Result |
|---|---|
| `tsc --noEmit` (strict, noUnusedLocals) | ✅ 0 errors |
| `vite build` | ✅ `dist/content.js` 251.5 KB (78 KB gzip), `dist/manifest.json` copied |
| CSS compiled & inlined | ✅ `.sai-glass`, `.fixed`, `prefers-reduced-motion` present in bundle; no raw `@import "tailwindcss"` |
| Manifest correctness | ✅ MV3, `content.js` referenced, YouTube-only matches |
| Remote code / eval / secrets | ✅ none in bundle |

### Manual checklist (Chrome acceptance environment)

| Area | Cases | Status |
|---|---|---|
| Extension | build · load unpacked · reload · YouTube refresh | ⏳ to be performed by reviewer (procedure above) |
| YouTube | homepage · standard video · direct URL · video→video navigation · refresh | ⏳ reviewer (detection logic unit-tested) |
| FAB | visible · clickable · hover · focus · keyboard (Enter/Space) · position | ⏳ reviewer (keyboard/ARIA covered by tests) |
| Overlay | open · close · minimize · restore · repeated cycles | ⏳ reviewer (behavior covered by 7 component tests) |
| CSS | YouTube intact · extension isolated | ⏳ reviewer (Shadow DOM guarantees isolation) |
| Responsive | 1280×720 · 1366×768 · 1440×900 · 1920×1080 | ⏳ reviewer (viewport-clamping math in CSS) |
| Failure | unsupported page · detection failure · init failure · nav during init | ✅ unit/component tested + fallback UI implemented |

## Definition of Done (Sprint 1)

- [x] Repository inspected before implementation (16 Sprint 0 docs; no prior code)
- [x] Sprint 0 documentation reviewed and followed
- [x] Chrome Manifest V3 extension builds; loads structure valid
- [x] YouTube pages detected (service + guard)
- [x] Current video ID identified (pure parser, never hardcoded, validated)
- [x] SPA navigation handled (event-driven, deduped, no polling)
- [x] Video context updates on change (store + UI reactive; tested A→B)
- [x] AI FAB exists, interactive, keyboard accessible, labeled
- [x] Futuristic transparent overlay exists (glass/blur/glow/HUD)
- [x] Overlay opens / closes / minimizes / restores (state preserved)
- [x] Overlay state architecture exists (7 states modeled)
- [x] Extension root isolated (Shadow DOM + namespaced CSS + single host)
- [x] CSS does not affect YouTube; YouTube styles cannot reach in
- [x] YouTube UI preserved (pointer-events pass-through, no page DOM edits)
- [x] Desktop responsive geometry (viewport-clamped)
- [x] Basic accessibility (labels, focus management, Escape, contrast, reduced motion)
- [x] Basic error states (init failure, unsupported, no video id, service error)
- [x] Development logging (init/detect/video/overlay/transitions, gated)
- [x] No fabricated AI results; no YouTube API; no backend; no DB; no mobile
- [x] Documentation updated (architecture, overlay-design, this file, README)
- [x] No unresolved build errors (`npm run verify` green)
- [x] Existing functionality intact (no prior code existed to break)

## Known Limitations (intentionally postponed)

1. **No real analysis** — Analyze ends in an honest "not connected yet" notice
   (`UnconfiguredAnalysisService`). Sprint 2 replaces it with the real API client.
2. **No title/channel/comments** — context is URL-derived only (Sprint 2).
3. **No background worker / service worker** — messaging module is a dormant
   typed boundary.
4. **No extension icon/branding assets** — manifest ships without `icons`.
5. **No store packaging/publication** — load-unpacked only.
6. **Theater-mode overlap** — in theater mode the panel covers the top-right of
   the video area (never controls); translucent by design; repositionable later.
7. **Fullscreen video** — elements outside the fullscreen element are hidden by
   the browser during native fullscreen; overlay/FAB follow standard extension
   behavior there.
8. **`yt-navigate-finish` reliance** — if YouTube renames its internal event,
   `popstate`/`hashchange` still cover history navigation; a URL-observer
   fallback is documented but not needed today (no polling by design).
9. **Manual Chrome load-test** — automated tests cover logic/behavior; the
   physical load-unpacked run in Chrome is the reviewer's acceptance step.

## Sprint 2 Readiness

See final report §11 — the `AnalysisService` seam, typed `VideoContext`,
state machine, and overlay state views are the exact attachment points for the
backend/API sprint.
