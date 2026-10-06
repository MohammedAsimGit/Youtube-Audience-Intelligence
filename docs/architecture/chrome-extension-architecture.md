# Chrome Extension Architecture (As-Built — Sprints 1–2)

This document describes the implemented client architecture. The Sprint 0 design
blueprint is preserved in §History at the end.

## Position in the System

The extension is the **presentation/client layer only** (Sprint 0 Rule 9). It
never performs comment retrieval, NLP, aggregation, or holds API keys.

```text
YouTube page
   └── Content script (detection + Shadow DOM mounting)
          └── React overlay UI (FAB, states, accessibility)
                 └── HttpAnalysisService → Backend API → YouTube Data API
                     (Sprint 2: wired; no keys in the client)
```

## Directory Structure (as implemented)

```text
├── package.json                # scripts: build, dev (watch), typecheck, test, verify
├── tsconfig.json               # strict TS, react-jsx, types: ["chrome"]
├── vite.config.ts              # single IIFE content bundle + vitest config
├── public/manifest.json        # Manifest V3 (copied verbatim to dist/)
├── dist/                       # ← load this folder unpacked into Chrome
│   ├── manifest.json
│   └── content.js              # single bundle (app + compiled CSS string)
└── src/
    ├── content/index.ts        # entry: guards, host+Shadow DOM mount, wiring, fallback
    ├── services/
    │   ├── youtube/detection.ts        # isYouTubeUrl() — the only detection logic
    │   ├── youtube/video-context.ts    # parseVideoContext() / getCurrentVideoContext()
    │   ├── youtube/spa-navigation.ts   # observeNavigation() — event-driven change detection
    │   ├── messaging.ts                # typed chrome.runtime boundary (future worker)
    │   └── analysis/
    │       ├── analysis-service.ts        # AnalysisService interface + offline stub
    │       └── http-analysis-service.ts   # real client → GET /api/videos/{id}
    ├── state/store.ts                    # status, minimized, context, notice, acquisition
    ├── state/useAppState.ts              # useSyncExternalStore bridge
    ├── ui/
    │   ├── App.tsx                # CLOSED⇄OPEN composition, Escape, focus return
    │   ├── mount.tsx              # createRoot helper
    │   ├── components/Fab.tsx     # AI floating action button
    │   ├── components/Overlay.tsx # shell: state machine wiring, focus, analyze flow
    │   ├── components/OverlayHeader.tsx # identity + status chip + minimize/restore/close
    │   ├── components/OverlayBody.tsx   # ready / analyzing / unsupported / error views
    │   ├── components/StatusChip.tsx    # HUD state pill + STATUS_LABELS
    │   ├── components/icons.tsx         # inline SVG icons
    │   └── styles/overlay.css           # Tailwind v4 + sai- design tokens (inlined into JS)
    ├── shared/types.ts            # VideoContext, OverlayStatus, OverlayState, messages
    └── lib/logger.ts              # gated, prefix-tagged logger
```

**Separation rule (enforced):** `YouTube detection → video context → application
state → React UI`. UI components contain zero YouTube DOM logic; the navigation
detector contains zero UI logic; both meet only in the store.

## Manifest V3 Configuration

`public/manifest.json` (copied to `dist/manifest.json`):

| Key | Value | Rationale |
|---|---|---|
| `manifest_version` | `3` | Current Chrome requirement. |
| `content_scripts.matches` | `https://www.youtube.com/*`, `https://youtube.com/*` | The **only** host access. Content-script `matches` grant injection directly — no separate `host_permissions` needed. |
| `content_scripts.js` | `["content.js"]` | Single pre-bundled client entry. |
| `run_at` | `document_idle` | Page (and YouTube's app root) is ready; avoids interfering with load. |
| `all_frames` | `false` | Top frame only; embedded players don't need the overlay. |
| `permissions` | **absent (none)** | The extension uses `fetch` + AbortController only - no Chrome APIs that require permissions. Every future permission must be justified here before addition. |
| `host_permissions` | `http://127.0.0.1/*`, `http://localhost/*` | **Sprint 2, loopback only:** content-script fetches to the local backend are cross-origin; the host permission exempts them from page CORS in MV3. YouTube access stays confined to `content_scripts.matches`; no broad hosts. |
| `background` | absent | No background worker needed for overlay-only UX. |
| `web_accessible_resources` | absent | Nothing is exposed to page scripts. |
| `icons` | absent | Optional; deferred (no design assets yet). |
| Secrets | **never** | No API keys anywhere in manifest or bundle. |

**Permission surface: loopback hosts only** (plus YouTube content-script
matches). Remote code: none (everything is bundled).

## Content Script Responsibilities (`src/content/index.ts`)

1. Guard: confirm the runtime URL is supported YouTube (defense-in-depth beyond
   manifest matches); skip mount and log a warning otherwise.
2. Compute the initial **video context** via `getCurrentVideoContext()` and push
   it into the store; log the detected video id.
3. Mount exactly one host element (`#sentiment-ai-extension-root`) with an **open
   Shadow Root**; inject the compiled stylesheet as a `<style>` node; render the
   React app into a namespaced container.
4. Subscribe to SPA navigation (`observeNavigation`) and update the store when
   the active video changes — logging `video changed`.
5. Guard against double-mounting (SPA never reloads, but re-injection is possible).
6. On any initialization failure: log the error and mount a tiny plain-DOM
   fallback panel with friendly copy ("AI Analyzer could not initialize. Please
   reload the page.") — no stack traces in the UI.

## React UI Responsibilities (`src/ui/**`)

- **FAB** — entry point; independent from the overlay lifecycle (closing the
  overlay re-renders the FAB, never unmounts the extension).
- **Overlay** — shell with header (identity, state chip, controls), notice
  banner, body state views, footer.
- **State model** — see below. The UI reads state only; actions call store methods.
- **Honest content** — no sentiment values exist anywhere in the codebase.
  Analyze calls `HttpAnalysisService` → `GET /api/videos/{id}`; success renders
  acquisition facts only (title, comment count, source, status), failures
  render categorized friendly errors - never fabricated numbers.

## State Architecture (`src/state/store.ts`)

```text
OverlayState = {
  status:        'closed' | 'open' | 'ready' | 'loading' | 'analyzing'
                 | 'complete' | 'error',   ← loading/complete/error exercised (Sprint 2),
                                            analyzing reserved for the sentiment pipeline
  minimized:     boolean,                  ← orthogonal flag
  videoContext:  VideoContext | null,
  notice:        string | null,            ← transient friendly message
  acquisition:   { status: 'idle'|'loading'|'success'|'error',
                   videoId, data?, errorCode?, errorMessage? }   ← Sprint 2
}
```

- Transition flow: **CLOSED → OPEN → READY → LOADING → COMPLETE | ERROR**
  (chip label for `complete` is "DATA ACQUIRED"; ERROR retries → READY).
  `analyzing` remains typed for the future sentiment pipeline.
- **Video change resets stale data + auto-opens (FR-11):** a different
  `videoId` clears `acquisition` + `notice` and **auto-opens the overlay for
  the new video** (`status → ready`, `minimized → false`) - Video A's data can
  never render for Video B, and no analysis starts automatically (READY only).
  Initial detection (page load) never auto-opens; leaving the video context
  (home/search) closes the overlay, FAB stays. In-flight responses are
  discarded by a stale guard in `Overlay.onAnalyze`.
- **Scroll-aware minimization:** while the expanded overlay is open, a
  passive capture `scroll` listener on `document` tracks YouTube page
  scrolling. A quick flick does NOT change anything - the transition needs
  **~1 s of sustained scrolling** (a scroll session: 300 ms without scroll
  events cancels it, and the checkpoint requires a scroll within the last
  200 ms). Scrolls whose target resolves to the extension's own UI (panel /
  shadow host) are ignored, so scrolling the analysis body never collapses
  the panel. The commit is **OPEN → MINIMIZED (`store.minimize()`), never
  OPEN → CLOSED**: the compact pill stays visible, the component stays
  mounted (job poll + results + video context intact), and clicking the
  pill restores the full panel. Already-closed and minimized states are
  no-ops, and page scrolls within a 750 ms **navigation grace**
  (`detectedAt` freshness) are ignored so YouTube's scroll-to-top during
  SPA navigation cannot dismiss the overlay. CLOSED remains reserved for
  the explicit ✕ button and Escape.
- **Minimize preserves state:** `minimized` flips without touching `status`,
  `videoContext`, `notice`, or `acquisition`, and the `Overlay` component stays
  mounted (only its body collapses) — nothing is destroyed or reset.
- Every transition is logged with from → to (`state (reason)` debug lines).
- External store + `useSyncExternalStore` lets the content script (navigation
  detector) push context changes without touching the React tree.

## Video Detection (`src/services/youtube/video-context.ts`)

- `parseVideoContext(href)` is a **pure function**: URL → `VideoContext
  { url, videoId, pageKind, isSupported, detectedAt }`.
- Video id is always parsed (`?v=` on `/watch`, segment after `/shorts/` and
  `/live/`), validated against the 11-character pattern `[A-Za-z0-9_-]{11}` —
  **never hardcoded**; malformed ids become `null`.
- Non-YouTube or unparseable URLs yield `pageKind: 'other'`, `isSupported: false`.
- `getCurrentVideoContext()` wraps the pure parser over `window.location.href`.
- Title/channel/comments are deliberately NOT collected (Sprint 2 scope).

## SPA Navigation Detection (`src/services/youtube/spa-navigation.ts`)

YouTube navigates without document reloads. Strategy — **event-driven, no
polling, no MutationObserver**:

| Event | Why |
|---|---|
| `yt-navigate-finish` (document **and** window) | The event YouTube dispatches at the end of SPA navigations; dual listeners cover both dispatch paths. |
| `popstate`, `hashchange` (window) | Browser-history fallback. |

Handlers compare the newly parsed context against the last one and fire the
callback **only on change** (double-fired events dedupe harmlessly). On change:
store update → UI re-renders with the new context (FR-10/FR-11 mechanism).
On video change the store also auto-opens the overlay for the new video with
cleared acquisition data/notice; initial detection never auto-opens (see
State Architecture). Analysis itself is never triggered automatically — only
by an explicit Analyze click.

## CSS Isolation Strategy — Shadow DOM (decision & rationale)

**Decision: open Shadow DOM on a single host element.**

- The full stylesheet is compiled by Tailwind at build time, imported as a JS
  string (`?inline`), and injected as a `<style>` into the shadow root — there
  is no stylesheet in YouTube's document at all.
- YouTube's page CSS cannot cascade into the shadow tree; extension CSS cannot
  leak out. No `body`/`button`/`div`/`*` globals exist in our CSS (element
  selectors are scoped under `.sai-root`).
- All classes are namespaced `sai-` as defense-in-depth.
- The host element itself gets only minimal **inline** styles (it lives in the
  page document, outside the shadow): positioning, z-index, pointer-events.
- Tailwind's preflight/reset applies only inside the shadow tree.

## Z-Index / Layering Strategy

```text
YouTube document
   └── #sentiment-ai-extension-root        z-index: 2147483647  (single ceiling)
          pointer-events: none             ← page stays fully interactive
          └── Shadow Root
                ├── .sai-root              (no z-index; document order stacking)
                │     ├── FAB              fixed, bottom-right
                │     └── Overlay          fixed, top-right
                │           ├── Header     (source order, no z-index)
                │           ├── Notice
                │           ├── Body       (scrollable)
                │           └── Footer
                └── interactive surfaces set pointer-events: auto
```

- Exactly **one** extreme z-index value exists (the host), required to sit above
  YouTube's highest layers (theater/menus). Internal layering uses **zero**
  z-index values — it is fully determined by source order inside the shadow
  tree, so it is predictable and cannot fight YouTube for stacking.
- No backdrops/scrims: the overlay is non-modal, so clicks outside it (including
  on YouTube) pass through untouched.

## Overlay Positioning & Responsive Behavior

- FAB: `fixed; right: 24px; bottom: 96px` — clear of the player's control bar
  and YouTube's corner controls; floats above page content during scrolling.
- Overlay: `fixed; top: 72px; right: 16px; width: 380px; height:
  min(640px, calc(100vh - 96px))`, `max-width: calc(100% - 2rem)` — cannot
  overflow the viewport at any desktop size; works at 1280×720, 1366×768,
  1440×900, 1920×1080, plus short (800×500) and narrow (400×700) viewports.
  Geometry is px-locked: never use rem in the viewport clamps (the host page
  owns the root font-size — YouTube sets 10px, which turned `6rem` into 60px
  and pushed the panel bottom 12px past the viewport), and prefer the
  containing block (`100%`) over `100vw`, which includes the classic
  scrollbar.
- Position is CSS-only and future-repositionable; nothing is pixel-hardcoded
  to one monitor.
- Known overlap: in theater mode the panel overlays the top-right of the video
  area (never the controls); the glass is translucent so content shows through.

## Accessibility Baseline

- Semantic `<button>` elements everywhere; meaningful `aria-label`s ("Open AI
  Analyzer", "Minimize/Restore AI Analyzer panel", "Close AI Analyzer").
- Overlay is `role="dialog"` with an accessible name; focus moves into the
  panel on open/restore, to the restore button on minimize, and back to the FAB
  on close.
- Escape closes the overlay from anywhere on the page.
- Visible `:focus-visible` outlines (ice-blue) on FAB, panel, and all controls.
- Notice banner is `role="status" aria-live="polite"`; Analyze exposes
  `aria-busy` while pending.
- Text contrast: light text (#e8eefc / slate-200/300) on dark glass ≥ 4.5:1;
  HUD labels use #9db3d9-class colors on near-black (≈7:1).
- `prefers-reduced-motion: reduce` disables entrance animations, transitions,
  and spinner rotation.

## Logging (`src/lib/logger.ts`)

Prefix `[sentiment-ai]`, levels debug/info/warn/error with a single
`MIN_LEVEL` switch to silence debug output for production. Logged events:
extension initialized, YouTube detected, video detected, video changed,
overlay mounted/opened/closed, every state transition. No personal data — only
public page identity (origin, pathname, video id).

## Performance

- One 261 KB bundle (80 KB gzip): React + app + compiled CSS string; built once,
  one lazy backend call per explicit Analyze (15s timeout, cached server-side).
- No polling, no MutationObserver, no continuous animation loops (entrance
  animations run once; hover transitions are CSS-only).
- Rendering happens only on store change (useSyncExternalStore); the navigation
  handler is deduped.
- One extra DOM element (host) + shadow children; pointer-events pass through.

## Security

- Zero secrets in the client; zero `eval`; no remote code or dynamic imports.
- Manifest permissions limited to loopback `host_permissions` (local backend
  only, documented above); no broad hosts, no optional permissions.
- All user-facing error copy is friendly; stack traces go to the console only.
- Backend keeps the YouTube API key server-side; tests assert it never appears
  in any response consumed by the extension.

## Backend Boundary (as-built, Sprint 2)

```text
Chrome UI → AnalysisService (src/services/analysis) → Backend API
              ├── HttpAnalysisService  (production wiring, content script)
              └── UnconfiguredAnalysisService (offline/dev stub, tests)
```

`AnalysisService.analyze({videoId, pageKind})` returns a discriminated union:
`success(data) | error(code, friendly message) | unavailable`. The content
script constructs `HttpAnalysisService` → `GET {origin}/api/videos/{videoId}`
(default `http://127.0.0.1:8000`, 15s AbortController timeout, origin
overridable via build-time `__SENTIMENT_AI_BACKEND__`). Contract details:
`docs/13-backend-api-contract.md`. `src/services/messaging.ts` remains the
typed `chrome.runtime` boundary for a future background worker (currently
returns null: no receiver).

## History

- Sprint 0 produced the design blueprint for this document (no code).
- Sprint 1 implemented it; decisions (Shadow DOM, event-driven SPA detection,
  external store, zero permissions) are as-built facts above.
- Sprint 2 wired the backend boundary: `HttpAnalysisService`, acquisition
  state + stale-data reset, LOADING/COMPLETED/ERROR views, loopback
  `host_permissions`. No sentiment analysis exists yet by design.
