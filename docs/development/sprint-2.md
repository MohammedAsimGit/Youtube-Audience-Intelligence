# Sprint 2 — YouTube Data Acquisition & Backend API Foundation

**Status:** complete (pending human end-to-end run with a real API key) · **Date:** 2026-09-24

## What This Sprint Delivers

```text
User opens YouTube video → extension detects id → Analyze → backend validates id
→ videos.list + paginated commentThreads.list (official API, server-side key)
→ validate + normalize → stable contract → overlay shows DATA ACQUIRED
```

Real data, **zero sentiment analysis** — the output is clean, structured
acquisition ready for the Sprint 3 pipeline/storage stage.

## Running the System

### Backend

```bash
cd backend
python -m venv .venv                       # once (already present in this checkout)
.venv/Scripts/python -m pip install -r requirements.txt   # once (Windows; use .venv/bin/python on unix)
cp .env.example .env                       # then put your real YOUTUBE_API_KEY in .env
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Verify: `curl http://127.0.0.1:8000/health` → `{"status":"ok","version":"0.2.0"}`
- Tests: `.venv/Scripts/python -m pytest` → **21 passed**
- Without a key the server still boots; `/health` works; acquisition returns
  honest `503 server_not_configured`.

### Extension

```bash
npm install
npm run build        # → dist/
npm run verify       # typecheck + 52 tests + build
```

Load `dist/` unpacked (chrome://extensions → Developer mode → Load unpacked),
open a YouTube video, click ✦ AI → **Analyze**.

### End-to-end checklist (§37 of the brief)

1. ✅ Start backend (uvicorn) 2. ✅ `/health` 200 3. ✅ build extension
4. ⏳ load extension 5. ⏳ open YouTube 6. ⏳ open a normal video
7. ⏳ open AI Analyzer 8. ⏳ click Analyze 9. ✅ LOADING state (tested)
10–14. ⏳ requires a real API key on your machine (the only step this
environment cannot perform — no key exists here by policy)
15. ✅ DATA ACQUIRED state with title/count/source/status (tested)
16–17. ✅ navigate → previous data cleared (tested, FR-11)
18. ⏳ analyze new video 19. ✅ backend-down → BACKEND UNAVAILABLE (tested)
20. ✅ invalid/unavailable video → categorized errors (tested)
21. ✅ comments unavailable → explicit state (tested)
22–23. ⏳ manual refresh/navigation passes through the same tested paths.

✅ automated in this repo · ⏳ manual runs requiring a real API key / Chrome UI.

## What Was Built

### Backend (`backend/`)

| Module | Responsibility |
|---|---|
| `app/main.py` | app factory, CORS (origins + extension regex), error handler, lifespan |
| `app/api/routes.py` | `GET /health`, `GET /api/videos/{video_id}` |
| `app/core/config.py` | pydantic-settings: key, cap=50, TTL=600s, CORS, log level |
| `app/core/errors.py` | categorized taxonomy (9 codes → HTTP statuses) |
| `app/core/logging.py` | structured secret-free logging |
| `app/clients/youtube.py` | **only** module touching Google; reason-mapped errors |
| `app/schemas/youtube.py` | external validation boundary (lenient but validated) |
| `app/models/internal.py` | project-owned camelCase contract |
| `app/services/acquisition.py` | validate → cache → metadata → paginated comments → normalize |
| `app/services/cache.py` | TTL + LRU seam (thread-safe, injected clock for tests) |
| `app/utils/normalize.py` | NFC/whitespace, parse_int/dt, thread→flat comments |
| `tests/` | 21 tests via fake-client injection (no network) |

### Extension (changes only — Sprint 1 untouched behavior preserved)

| File | Change |
|---|---|
| `shared/types.ts` | + acquisition contract types, `AcquisitionState` in `OverlayState` |
| `state/store.ts` | + begin/complete/fail/clear acquisition; video-change resets stale data |
| `services/analysis/analysis-service.ts` | result union gains `success(data)` / `error(code,msg)` |
| `services/analysis/http-analysis-service.ts` | **new** real client (15s timeout, origin override hook) |
| `ui/components/Overlay.tsx` | new analyze flow, stale guard, footer "Sprint 2" |
| `ui/components/OverlayBody.tsx` | Acquiring / Acquired / categorized Error / Comments-unavailable views |
| `ui/components/StatusChip.tsx`, `OverlayHeader.tsx` | `complete` renders as "DATA ACQUIRED" |
| `content/index.ts` | wires `HttpAnalysisService` |
| `public/manifest.json` | + loopback `host_permissions` (documented), v0.2.0 |
| tests | +8 HTTP-service tests, +5 acquisition store tests, +5 flow tests (52 total) |

## Test Results (all green)

| Suite | Result | Covers |
|---|---|---|
| Backend pytest | **21 passed** | health, invalid ids, not-found, contract shape, pagination + replies, cap/hasMore, comments-disabled soft state, quota 429, timeout 504, malformed payloads, cache hit (0 extra calls), API-key never in responses |
| Extension `npm run verify` | **typecheck 0 · 52/52 · build ✅** | Sprint 1 suite intact; loading→DATA ACQUIRED, categorized error + retry, stale-data clearing, comments-unavailable, disabled-comments non-error, HTTP mapping (success/envelope/non-JSON/unknown code/malformed body/network/timeout/origin override) |
| Live backend run | ✅ | `/health` 200, invalid id 422, missing key → honest 503, CORS preflight 200 for extension origin + ACAO echo, foreign origin → no ACAO |
| Secret scan | ✅ | no `AIza…`-like strings anywhere; `backend/.env` absent; `.gitignore` covers `.env` |

## Definition of Done (Sprint 2)

Backend: [x] runs · [x] /health · [x] env config · [x] key server-side ·
[x] id validation · [x] official API integrated · [x] metadata · [x] comments
where available · [x] pagination · [x] external validation · [x] normalization ·
[x] contract documented · [x] errors · [x] logging · [x] cache boundary.
Extension: [x] Sprint 1 intact · [x] Analyze → backend · [x] videoId sent ·
[x] loading · [x] success · [x] error · [x] real acquisition info ·
[x] no fake sentiment · [x] video change clears data.
Security: [x] key invisible to browser (asserted by test) · [x] .env ignored ·
[x] no secrets committed · [x] input validation · [x] CORS configured (verified).
Testing: [x] backend tests · [x] extension builds/tests · [x] detection & SPA
still green · [x] failure states · [x] no unresolved build/runtime errors in CI.
Documentation: [x] docs 11/12/13 · [x] decision record · [x] README ·
[x] known limitations.

## Known Limitations (intentionally unfinished)

1. **No sentiment/AI anything** — by design (Sprint 4).
2. **No persistence** — collected comments live only in the response; STORE stage is Sprint 3 (TDR-06 open).
3. **In-memory cache only** — single process, lost on restart.
4. **Cap = 50 comments/request** — config-driven; raised later when quota + storage allow.
5. **`duration` null** — needs `part=contentDetails` if ever required.
6. **Replies within cap** — deep reply chains not fully expanded.
7. **Sync httpx client** — fine at MVP volume; async only if measured.
8. **No real-key E2E run here** — no YouTube API key exists in this environment (by policy); the exact manual path is documented above for the reviewer with a key.
9. **Auth/rate-limiting of our own API** — none yet (localhost trust model); revisit before any non-local deployment.

## Sprint 3 Readiness

- Normalized `Comment` records + `VideoMetadata` are the exact rows Sprint 3 stores (TDR-06 decision + schema design plug directly into `VideoDataService`).
- The acquisition seam (`VideoDataService.get_video_data`) is where a `STORE` stage hooks in without touching routes or UI.
- `comments.hasMore` / cap / `source.retrievedAt` give the crawl window semantics needed for incremental collection.
- Cache seam documented for replacement by persistent cache/queue (TDR-07).
- Error taxonomy + overlay states already cover pipeline failure UX (FR-12).
