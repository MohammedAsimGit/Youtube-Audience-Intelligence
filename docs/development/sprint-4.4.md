# Sprint 4.4 — Zero-Click YouTube Intelligence & Automatic Local Backend Startup

**Status:** complete · **Date:** 2026-09-28 · **Version:** 0.4.4

## What This Sprint Delivers

```text
Open Chrome → open YouTube → backend becomes available automatically
→ select a video → extension detects it → analysis starts by itself
→ comments acquired → sentiment processed → results appear
```

No terminal, no Analyze button. The video ID is the only trigger; **Retry**
(from a failure state) is the only manual control. Backend availability is
proven by `GET /health` — never by "the port is open" — and on Windows the
local FastAPI service is started on demand by a Chrome Native Messaging
host (`launcher/`), idempotently and detached (no console window).

## User Flow (§49)

1. Open Chrome (extension loaded, native host registered once).
2. Open YouTube — the content script detects the page.
3. Pick/open a video (direct URL, search, recommendation, SPA navigation).
4. The overlay **auto-opens** (READY), then shows **CONNECTING** while the
   availability gate verifies/starts the local backend (bounded).
5. Analysis starts automatically: **QUEUED → ACQUIRING (real counts) →
   ANALYZING (real counts) → ANALYSIS COMPLETE**.
6. Switch videos → the new video's run starts by itself; the old run is
   cancelled/ignored (stale results can never render).
7. If something fails → friendly FAILED copy + **Retry** (the only button;
   never connection details, ports, or stack traces).

## Developer Flow (§49)

Dev mode is unchanged and coexists with the automatic path:

```bash
# terminal 1 — manual backend (still fully supported)
cd backend && .venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# terminal 2 — extension
npm run verify        # typecheck + tests + build → dist/
```

The gate's fast path sees the healthy `/health` answer and **never calls
the native host**, so dev machines need no registration. To exercise the
launcher instead: stop uvicorn and register the host once
(`python launcher/register_host.py --extension-id <id>`); the next video
detection starts the backend itself.

Tests: `.venv/Scripts/python -m pytest` → **256 passed, 4 skipped**;
`npm run verify` → **142 extension tests** (both green);
`RUN_REAL_API_TESTS=1 pytest tests/test_real_api.py` → **4 passed**.

## Launcher Architecture (§49)

```text
CHROME EXTENSION (MV3, no shell exec)
   │  connectNative("com.sentiment_ai.backend") → {"action":"ensure_backend"}
   ▼
LOCAL HOST (launcher/native_host.py, stdlib, Chrome native framing)
   │  1. GET /health ok?            → already_running   (idempotent, §34)
   │  2. port busy, /health not ok? → port_in_use (never double-bind)
   │  3. spawn DETACHED uvicorn (backend\.venv python, cwd=backend/)
   │  4. poll GET /health (bounded 20 s) → started | timeout
   ▼
FASTAPI (127.0.0.1:8000, GET /health = readiness truth, §35) → SQLITE
```

Registration (once per machine/profile, HKCU, no admin):
`python launcher/register_host.py --extension-id <32-char id>` writes
`launcher/native-host-manifest.json` (relative `path: host.bat`,
`allowed_origins` locked to that one extension) and the HKCU
`NativeMessagingHosts\com.sentiment_ai.backend` key. Full protocol,
extension-side gate and troubleshooting: 
[docs/architecture/local-backend-launcher.md](../architecture/local-backend-launcher.md).

## What Was Built

- **`src/state/store.ts`** — any detected video id auto-opens the overlay
  (READY) and resets slices (boot detection included); same-id context
  refreshes are deduped (§7/§24): one video → one run.
- **`src/ui/components/Overlay.tsx`** — automatic pipeline: availability
  gate → background job → results, triggered **only** by video detection
  (autoHandledRef, once per id) and by Retry from a failure state (§23).
  Gate-stale and job-stale guards; superseded runs can't clear the current
  run's polling handle.
- **`src/services/backend/backend-gate.ts`** — `NativeBackendGate`:
  `GET /health` → `{"action":"ensure_backend"}` via native messaging →
  bounded health re-probe (6×500 ms, 25 s cap), one shared in-flight
  promise, friendly §30 copy on failure.
- **`src/ui/components/OverlayBody.tsx`** — `ConnectingView` (honest
  CONNECTING, no fake %), `AwaitingAnalysisView`; **Analyze /
  Analyze-again / Run-analysis controls removed** — Retry exists only in
  error states.
- **`launcher/`** — `native_host.py` (strict protocol, idempotent
  `ensure_backend`, detached spawn, native framing), `host.bat`
  (venv-relative, PATH-python fallback), `register_host.py` (HKCU
  registration, `--unregister`), manifest `nativeMessaging` permission,
  version **0.4.4** across manifest/package/pyproject/API/README.
- **`content/index.ts` / `App.tsx` / `mount.tsx`** — gate threading;
  production wires `NativeBackendGate` into the overlay.

## Verification (§51)

| Check | Result |
|---|---|
| Backend suite | ✅ **256 passed, 4 skipped** (238 pre-existing + 18 native-host) |
| Real-API suite | ✅ **4 passed** (one earlier run hit a transient YouTube retry storm — flaky network, not code) |
| Extension verify | ✅ typecheck + **142 tests** (10 files) + production build |
| Automatic backend startup (real) | ✅ `ensure_backend` on a free port spawned a detached uvicorn, health-polled to `{"status":"ok","version":"0.4.4"}` in ~8 s |
| Idempotency (real) | ✅ second call → `already_running` in 0.6 s; existing 8000 backend reused, never duplicated |
| Failure/recovery (real) | ✅ killed the spawned backend → health DOWN → `ensure_backend` respawned it healthy |
| Strict protocol (real) | ✅ unknown action → `{"ok":false,"reason":"unknown_action"}` |
| Canonical service | ✅ 8000 instance cycled through the launcher; `/health` now reports 0.4.4 |
| Clean-session open, video switching, 5 000-comment video | ⏳ manual in Chrome (needs a real browser + YouTube; automated equivalents: auto-analysis.test.tsx navigation/boot/rapid-switch tests + Sprint 4.3 large-video timing) |

## Acceptance Checklist (§50)

- [x] No Analyze / Analyze-again / Run-analysis control in the normal flow (§3/§23) — asserted in tests via `assertNoManualAnalyzeControls()`
- [x] Analysis auto-starts on video detection — boot + SPA navigation (§4/§17)
- [x] Duplicate events for one video → exactly one job (§7/§24)
- [x] Rapid A→B→C→D → only D controls the overlay; late results dropped (§18/§41)
- [x] Backend availability via `GET /health`, not port-open (§35)
- [x] Automatic startup via native messaging, strict `ensure_backend` only, no shell exec from the extension (§33)
- [x] Idempotent backend start; bounded retries/timeouts everywhere (§15/§34)
- [x] Honest CONNECTING state; no fake percentages (§19/§21)
- [x] Friendly failure copy + Retry-only-in-error (§23/§30)
- [x] Sprint 4.2/4.3 behavior preserved (job dedupe, stale guards, cancel, STALE sweep)
- [x] Version 0.4.4 everywhere; docs + changelog updated (§49)
- [x] Full test suites green with real counts (256 backend / 142 extension / 4 real-API)
- [ ] Manual clean-session Chrome run (§51) — pending human step below

## Known Limitations

- Native-host registration is **Windows-only** (HKCU; `register_host.py`
  says so explicitly). Elsewhere the gate degrades to friendly FAILED +
  Retry while manual/dev uvicorn still works via the health fast path.
- The extension id must be re-registered whenever the unpacked `dist/`
  path changes (Chrome derives unpacked ids from the path).
- Chrome blocks native messaging for extensions loaded from some
  web-store contexts; unpacked dev loads are unaffected.
- The large-video and clean-session checks need a real browser/YouTube
  session; automated coverage exercises the same code paths with fixtures.

## Troubleshooting (§49)

| Symptom | Fix |
|---|---|
| BACKEND UNAVAILABLE / "Local AI service is unavailable" | Press **Retry**. Persistent: start uvicorn manually or fix native registration (below). |
| Native host never fires | Re-register: `--unregister` then `--extension-id <id>`; verify id matches `chrome://extensions`. |
| Launcher spawn fails | Check `backend/data/launcher-uvicorn.log`; ensure `backend/.venv` exists (or python + deps on PATH). |
| Port 8000 owned by a foreign process | Gate trusts only `/health`; stop the squatter or set `BACKEND_PORT` on both sides. |
| Permission/policy blocks native messaging | Dev fallback: run uvicorn yourself — the health fast path needs no host. |
| Stale job after restart | Startup sweep marks orphaned jobs STALE → Retry (Sprint 4.3 behavior, unchanged). |
| Failed analysis | Categorized cause in the overlay + **Retry**; stored comments are kept and deduped. |
