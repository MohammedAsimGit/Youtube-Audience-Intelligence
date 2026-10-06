# Local Backend Launcher — Chrome Native Messaging Host (Sprint 4.4)

**Status:** implemented (`launcher/`, tests in `backend/tests/test_native_host.py`) · **Date:** 2026-09-28
**Scope:** making the local FastAPI backend *available automatically* so end
users never open a terminal. No other process-spawning component exists in
the system.

## Why

Before Sprint 4.4 the user had to start the backend by hand before the
extension could do anything. The target experience is:

```text
Open Chrome → open YouTube → pick a video → results appear
```

The extension itself can never execute shell commands (browser sandbox,
§33), so process startup is delegated to Chrome's supported **Native
Messaging** channel: the extension may *ask* a registered local host to do
one fixed thing — make sure the backend is running.

## Architecture

```text
CHROME EXTENSION (MV3, sandboxed - no shell exec, no raw sockets to spawn)
    │  chrome.runtime.connectNative("com.sentiment_ai.backend")
    │  {"action": "ensure_backend"}                 (strict, closed protocol)
    ▼
LOCAL HOST / LAUNCHER (launcher/native_host.py, stdlib-only Python)
    │  1. GET http://127.0.0.1:8000/health  → ok?  → "already_running"
    │  2. port busy but /health not ok?     → "port_in_use" (never double-bind)
    │  3. spawn DETACHED uvicorn (backend/.venv python, cwd=backend/)
    │  4. poll GET /health until status=ok   → "started"  (bounded, 20 s)
    ▼
FASTAPI BACKEND (app.main:app, 127.0.0.1:8000, GET /health = readiness truth)
    ▼
SQLITE (backend/data/sentiment.db - unchanged data layer)
```

Key properties:

- **§34 idempotent** — a healthy backend is reused, never restarted or
  duplicated. Concurrent `ensure_backend` calls collapse to one spawn
  (extension side shares one in-flight promise; host side checks health
  before spawning).
- **§35 health, not port** — readiness is decided exclusively by
  `GET /health` returning `{"status":"ok", ...}`. A foreign process on
  port 8000 that does not answer `/health` counts as unavailable (and the
  host refuses to spawn a second backend that would fail to bind anyway).
- **§12 detached** — on Windows the backend starts with
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`: no console window, no
  terminal, survives the native host's exit. Logs append to
  `backend/data/launcher-uvicorn.log`.
- **§31/§36 canonical address** — `127.0.0.1:8000`, overridable via
  `BACKEND_HOST` / `BACKEND_PORT` for both host and extension. All paths
  are computed relative to the launcher file; no developer-specific
  absolute paths live in the repo.
- **§33 strict protocol** — only `{"action": "ensure_backend"}` is
  understood; messages are validated and arbitrary commands are rejected,
  never executed. The manifest's `allowed_origins` restricts the host to
  exactly one extension id.

## Wire protocol

Native messaging frames every message with 4-byte little-endian length +
UTF-8 JSON, stdin/stdout both ways.

```text
extension →  {"action": "ensure_backend"}

host     →   {"ok": true,  "outcome": "already_running" | "started", "port": 8000}
         |   {"ok": false, "reason": "port_in_use" | "timeout" | "spawn_failed"
         |                       | "health_failed" | ...}
```

The extension never reads the host's answer as *truth*: after any host
reply it re-probes `GET /health` itself (bounded: 6 attempts × 500 ms,
1.5 s timeout each, 25 s cap on the native call). Health on the browser
side is the only thing that flips the gate to `ready`.

## Extension-side gate (src/services/backend/backend-gate.ts)

```text
ensure():
  1. GET /health → ok?      → ready{already_running}      (fast path, no native call)
  2. connectNative {"action":"ensure_backend"}            (bounded, 25 s)
  3. poll GET /health       → ready{started} | unavailable{health_failed}
     (any native error/timeout → unavailable{native_*}; NOTHING retries forever)
```

Failure produces the friendly §30 copy — *"Local AI service is
unavailable. Please restart the AI service and press Retry."* — never
`127.0.0.1`, `:8000`, `connection refused`, stack traces or fetch errors.
Retry (the only manual control, failure states only) re-runs the whole
gate; it is not an automatic loop.

## Registration (once per Windows machine / Chrome profile)

```bash
python launcher/register_host.py --extension-id <32-char-id-from-chrome://extensions>
python launcher/register_host.py --unregister      # cleanup
```

What it writes (paths computed at runtime):

1. `launcher/native-host-manifest.json` —
   `{"name": "com.sentiment_ai.backend", "path": "host.bat", "type": "stdio",
     "allowed_origins": ["chrome-extension://<id>/"]}` (relative `path` is
   resolved against the manifest's directory, so the checkout stays portable).
2. `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.sentiment_ai.backend`
   → default value = absolute path of that manifest.

`launcher/host.bat` runs `%~dp0..\backend\.venv\Scripts\python.exe` when the
project venv exists, otherwise falls back to `python` on PATH. Registration
is Windows-only by design (`register_host.py` exits with a clear message
elsewhere); the extension's gate degrades to `native_unavailable` →
friendly FAILED + Retry when no host is registered, so dev setups that run
uvicorn manually are unaffected.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| Overlay stuck then shows **BACKEND UNAVAILABLE** | The gate could not reach or start the backend. Click **Retry**; if it persists, start uvicorn manually (README Quick start) or fix native registration below. |
| Native host never runs (no `launcher-uvicorn.log` entry) | Re-register: `--unregister` then `--extension-id <id>`; confirm `chrome://extensions` id matches the one you registered (unpacked ids change if the `dist/` path changes); confirm the manifest `name` is `com.sentiment_ai.backend`. |
| Host runs but backend never becomes healthy | Check `backend/data/launcher-uvicorn.log` (uvicorn errors, missing `.env`). `host.bat` needs `backend/.venv` (or python on PATH with `fastapi`/`uvicorn` installed). |
| **Permission issues** | Registration writes only to `HKCU` (current user, no admin). Corporate policy may block native messaging hosts entirely → use the dev fallback (start uvicorn yourself; the gate's fast path detects it). |
| **Port conflicts** | Another process owns 8000: host returns `ok:false, reason:"port_in_use"` (it never double-binds); the gate reports health_failed unless that process actually answers `/health`. Stop the squatter or set `BACKEND_PORT` for both sides. |
| **Startup failure** (spawn error, venv missing) | `reason:"spawn_failed"` → fix the venv (`python -m venv backend/.venv && pip install -r backend/requirements.txt`) and Retry. |
| Slow first open | Expected: bounded health re-probes (≤ ~1.5 s each) while uvicorn boots. CONNECTING is shown the whole time — the UI never freezes and never fakes a percentage. |

## Tests

- `backend/tests/test_native_host.py` — **18 tests**: strict protocol
  (unknown action/invalid payload rejected), idempotent `ensure_backend`
  (healthy reuse, no spawn; spawn-then-poll; port-busy-without-health →
  `port_in_use`; bounded timeout; spawn failure), native framing round-trip,
  manifest/registry registration (with `--extension-id` validation and
  `--unregister`), health-check semantics (`status != ok` → down).
- Extension: `src/services/backend/backend-gate.test.ts` — **8 tests**
  (fast path skips native, one shared in-flight `ensure`, message shape
  `{action:"ensure_backend"}`, bounded failures, health-not-port §35,
  friendly-copy guard) + `src/ui/auto-analysis.test.tsx` — **7 tests**
  (zero-click start, duplicate-event dedupe, boot auto-start, rapid
  A→B→C→D, CONNECTING lifecycle, failure + Retry recovery).
