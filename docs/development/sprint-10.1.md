# Sprint 10.1 — Android Foundation & Overlay Runtime

**Status:** complete · **Date:** 2026-10-03 · **Version:** 0.10.1

## What This Sprint Delivers

A **brand-new, fully isolated Android client** under `mobile/` (Flutter UI +
native Kotlin runtime) that provides everything the future YouTube AI overlay
needs at the platform level: overlay-permission management with a single
source of truth, a foreground `OverlayService` that owns a minimal test badge
drawn above other apps, **idempotent start/stop**, a central six-state
`OverlayRuntimeState` machine shared verbatim between Kotlin and Dart, a
thin MethodChannel/EventChannel bridge with §27 friendly errors, and the §18
permission setup screen with foreground re-check. The desktop extension and
the backend are **byte-for-byte untouched** and both regression gates pass
(§9). This is *foundation only*: **no YouTube video detection, no
Accessibility Service, no intelligence** — those are Sprint 10.2+ work.

Two behaviors are deliberate and documented (§5, §11): the overlay **never
auto-restarts** (the service is `START_NOT_STICKY` and only explicit user
actions start it), and **duplicate start/stop calls are safe no-ops**.

## 1. Inspection (no guessing)

| Question | Answer (verified) |
|---|---|
| Existing Android/Flutter/Dart/Kotlin code? | **None** — `find`/`grep` across the repo found zero matches; the Android client is greenfield, created with `flutter create --org com.sentimentai --project-name ai_overlay --platforms android mobile` |
| Existing mobile pieces to reuse? | None exist; the Android client reuses the **backend unchanged** (`http://10.0.2.2:8000` from the emulator, no new endpoints) and the desktop stays the protected reference client |
| Anything outside `mobile/` changed? | **No** — the entire delivery lives under `mobile/` plus this document and a README section (§9) |
| Toolchain | Flutter 3.41.9 / Dart 3.11.5 (`C:\flutter`), Android SDK (platforms 34–36), Gradle 8.14 wrapper, AGP 8.11.1, Kotlin 2.2.20, Java 17 targets — versions chosen by `flutter create`, **not upgraded** (§6) |
| JDK constraint | PATH only has Java 8 (AGP-incompatible) and the IDE JBRs are Java 25, which **crashes Gradle 8.14's Kotlin-DSL compiler** (`IllegalArgumentException: 25.0.4`). Working JDK: the VS Code Java extension's Temurin **JDK 21.0.12.1** — used for every Gradle/Flutter Android build in this sprint (§6) |
| Constraints honored | Desktop protection, no backend duplication, no API keys in the APK, no Accessibility Service, no video detection, no intelligence in service/notification, explicit start/stop only (§39 stop — Sprint 10.2 not started) |

## 2. Architecture & why

```text
Flutter (Dart UI, §18 setup screen)
   │  MethodChannel  sentimentai/overlay        (5 methods)
   │  EventChannel   sentimentai/overlay_events (state envelopes)
   ▼
OverlayBridge  ── thin adapter only, no logic
   ▼
OverlayController   ← ONE state machine, pure Kotlin, JVM-tested
   │  seams: hasPermission() · OverlayServiceGateway · openSettings() · log()
   ▼
OverlayService (foreground, START_NOT_STICKY)
   │  onCreate → notification + badge add  (once — duplicate-proof)
   │  onDestroy → badge remove + state report (every exit path)
   ▼
WindowManager  TYPE_APPLICATION_OVERLAY  →  drawn above the YouTube app
```

| File | Role |
|---|---|
| `mobile/lib/core/platform/overlay_platform.dart` | The ONE Dart-side abstraction: channel names, wire parsing (`OverlayRuntimeState.tryParse` — unknown states never crash), §27 friendly error mapping |
| `mobile/lib/features/overlay/presentation/overlay_home_page.dart` | §18 setup screen: permission card + service card + error banner; re-checks permission on every resume (§7.4) |
| `mobile/lib/core/logging/app_logger.dart`, `mobile/lib/core/config/app_config.dart` | §28 event logging + backend base URI (no secrets) |
| `overlay/OverlayRuntimeState.kt` / the Dart enum | Shared six-state model — wire strings identical by contract |
| `overlay/OverlayPermissionManager.kt` | **The only place** `Settings.canDrawOverlays` is called (§25) |
| `overlay/OverlayController.kt` | Central state machine + process-wide `attach()` factory (§4) |
| `overlay/OverlayService.kt` + `ServiceOverlayGateway` | Foreground service, notification, window lifecycle (§5) |
| `overlay/TestOverlayBadge.kt` | The deliberately-minimal "AI" badge (§5) |
| `overlay/OverlayBridge.kt`, `MainActivity.kt` | Channel adapter; activity attaches the controller, re-checks on resume, detaches on destroy |

Naming note: the brief's `OverlayState` collides with Flutter's own
`OverlayState` (widgets/overlay), so both languages call the model
**`OverlayRuntimeState`** — same six values, no ambiguity.

## 3. Overlay permission (one source of truth)

- `OverlayPermissionManager` is the **single** permission authority (§25):
  `hasPermission()` wraps `Settings.canDrawOverlays`, `openSettings()` fires
  `ACTION_MANAGE_OVERLAY_PERMISSION` (package-scoped) and swallows an absent
  settings activity instead of crashing.
- The controller **reconciles on every query**: `permissionGranted()` detects
  flips, logs `overlay_permission_changed granted|revoked` (§28 vocabulary),
  and when permission was revoked mid-run it stops the service and lands on
  `PERMISSION_REQUIRED` — never a zombie overlay (§27).
- `MainActivity.onResume → onAppForeground()` re-checks on every
  foreground transition, so a grant made in the system settings screen (or a
  later revocation) is reflected without restarting the app (§7.4).
- Dart never calls Android permission APIs directly — it asks the native
  source of truth and mirrors the answer.

## 4. Central state machine (`OverlayController`)

| State | Meaning |
|---|---|
| `DISABLED` | service stopped, permission granted — startable |
| `PERMISSION_REQUIRED` | overlay permission missing/revoked |
| `STARTING` | start dispatched, service not yet confirmed |
| `ACTIVE` | service running, badge window exists |
| `STOPPING` | stop dispatched, teardown not yet confirmed |
| `ERROR` | last start failed; recoverable by retry |

Guarantees (each pinned by `OverlayControllerTest`, §8):

- **Idempotent start (§14):** already running → answers `ACTIVE`, the
  gateway is dispatched exactly once; no permission → `Failed("permission_denied",
  "Overlay permission is required.")` with **zero** service dispatches.
- **Idempotent stop (§14):** stopping a stopped runtime answers the
  resting state, never an error, never a gateway call.
- **Stop while `STARTING`:** resolves synchronously (no callback may ever
  come); if the creation still races through, `onServiceCreated()` in
  `STOPPING`/`ERROR`/`PERMISSION_REQUIRED` **tears the window down again**.
- **Unexpected termination:** `ACTIVE` + dead service degrades to `DISABLED`
  on the next query; a ghost running service the UI never saw is adopted as
  `ACTIVE` (engine restart).
- **`ERROR` is preserved** through `onServiceStopped` so a failure stays
  visible until the user retries (§27).
- Every transition is pushed to the Flutter event stream — the UI never
  polls stale truth.

## 5. Foreground service, window and the minimal overlay

- **`START_NOT_STICKY` (§19, explicit by design):** after the system or the
  user kills the process, **nothing restarts the overlay**; it comes back
  only through an explicit *Start AI Overlay* tap. This is the documented,
  predictable behavior — not an omission.
- Foreground with `FOREGROUND_SERVICE_TYPE_SPECIAL_USE` on API 34+ (plus the
  matching permission and `PROPERTY_SPECIAL_USE_FGS_SUBTYPE`), channel
  `overlay_runtime` at `IMPORTANCE_LOW` (silent), notification id 41, copy
  from `strings.xml` — no intelligence ever appears in the notification.
- **Duplicate starts cannot duplicate the window:** Android creates one
  service instance per process and `onCreate` adds the badge exactly once;
  extra `onStartCommand` intents only return `START_NOT_STICKY`.
- **Every exit path funnels through `onDestroy`** (stop, revocation,
  foreground failure, window failure) so `WindowManager` references are
  always released; `isRunning` is the `@Volatile` lifecycle flag the
  controller consults.
- **The badge** (`TestOverlayBadge`): a small round "AI" TextView
  (`TYPE_APPLICATION_OVERLAY`, `FLAG_NOT_TOUCH_MODAL`) anchored bottom-end
  with a 16dp margin — the smallest practical footprint that still proves
  §11: touches **outside** it pass through to the app below (YouTube stays
  usable), a tap on it answers with a toast (interactivity proof), and
  removal is a single `removeViewImmediate`.

## 6. Flutter ↔ Kotlin bridge

| Method | Answer |
|---|---|
| `getOverlayPermissionStatus` | `bool` — native truth only |
| `requestOverlayPermission` | `bool` — opens the system screen; the confirmed state arrives via the next resume re-check |
| `getOverlayState` | wire string (`DISABLED…ERROR`) |
| `startOverlay` / `stopOverlay` | wire string, or `error(code, friendlyMessage)` |

Error codes → §27 copy (native detail never reaches the UI):
`permission_denied` → "Overlay permission is required." ·
`start_failed` → "Unable to start AI overlay." ·
`unavailable` (missing plugin) → "Unable to start AI overlay." ·
anything else → "Something went wrong. Please try again."

Events: `{'event': 'overlay_state', 'state': '<WIRE>'}` envelopes; unknown
envelopes **and unknown state values are skipped, never fatal** (§17
forward-compat — an older Flutter layer survives a newer native layer).

## 7. Permission / service UX (§18)

- Exact §18 copy: "Enable AI Overlay", "✓ Enabled" / "○ Required",
  **[Enable Overlay]**, **[Start AI Overlay]** / **[Stop AI Overlay]**.
- The start button is only reachable when permission is granted; the
  current central state is rendered as its wire value (monospace, colored:
  green ACTIVE, red ERROR, amber PERMISSION_REQUIRED).
- Failures surface in a friendly error banner (never a stack trace); the
  footer states this is a technical foundation — YouTube detection and the
  AI experience arrive in later sprints.

## 8. Testing (§29)

| Suite | Command | Result |
|---|---|---|
| Flutter analyze | `cd mobile && flutter analyze` | **No issues found** |
| Flutter tests | `cd mobile && flutter test` | **16/16 passed** (9 channel-contract + 7 widget) |
| Kotlin JVM tests | `cd mobile/android && ./gradlew test` | **19/19 passed** (see below) |
| Android APK | `cd mobile/android && ./gradlew assembleDebug` | **BUILD SUCCESSFUL** |

Dart coverage: channel tests pin all five methods, wire parsing (including
unknown-state → friendly error, never a cast crash), §27 mapping for
`permission_denied`/`start_failed`/missing-plugin, and the event stream
filtering (known states yielded, unknown/envelope-mismatched payloads
skipped). Widget tests cover both permission branches, start/stop flows,
event-driven state display, the error banner + ERROR refresh, and the
smoke render.

Kotlin coverage (`OverlayControllerTest`, pure JVM — fakes for permission,
gateway, settings and the §28 log sink): initial-state matrix; start with /
without permission; dispatch failure → ERROR; duplicate start dispatches
once; active/quiet/during-STARTING stop; creation racing a stop; service
callbacks; ERROR preservation; permission revocation while ACTIVE (stops the
service, logs `overlay_permission_changed`); ghost-service adoption;
unexpected-termination degradation; foreground reconciliation; and the exact
wire-string contract shared with Dart.

<!-- VERIFICATION -->

## 9. Desktop regression gate (protected, unchanged)

| Suite | Command | Result |
|---|---|---|
| Backend | `cd backend && ./.venv/Scripts/python.exe -m pytest -p no:warnings -q` | exit 0 — **505 passed, 4 skipped** (pre-existing real-API skips) |
| Extension | `npm run verify` | exit 0 — typecheck clean, **230/230 tests**, build `dist/content.js` 369.62 kB |
| Change scope | `git status` | every change lives under `mobile/` + `docs/` + this README section; `src/`, `backend/`, `package.json`, `preview/` untouched |

## 10. Build environment notes (this machine)

Two environment quirks had to be solved **without changing project files**:

1. **JDK** — Gradle must run on JDK 17/21, not the Java 8 on PATH nor the
   Java 25 IDE JBRs (Java 25 breaks Gradle 8.14's Kotlin-DSL script
   compiler: `IllegalArgumentException: 25.0.4`). Every Android build in
   this sprint used:

   ```bash
   JDK21="$HOME/.vscode/extensions/redhat.java-1.56.0-win32-x64/jre/21.0.12.1-win32-x86_64"
   cd mobile/android && JAVA_HOME="$JDK21" ./gradlew test assembleDebug
   ```

2. **Slow Flutter engine host** — `storage.googleapis.com/download.flutter.io`
   served ~14 KB/s here, stalling Gradle for hours on the 390 MB of debug
   engine ABI jars. Fix (no repo changes): seed a **local Maven mirror**
   with chunked parallel downloads from the official
   `storage.flutter-io.cn` mirror, then point Gradle at it through the
   plugin's own supported hook:

   ```bash
   FLUTTER_STORAGE_BASE_URL=file:///C:/Users/Admin/.gradle/flutter-engine-base \
     ./gradlew test assembleDebug
   ```

   (`FLUTTER_STORAGE_BASE_URL` is read by Flutter's Gradle plugin; the
   realm is empty, so the repo becomes
   `…/flutter-engine-base/download.flutter.io`. The mirror holds the POM +
   jar for `flutter_embedding_{debug,release}` and all six ABI jars.)

## 11. Known limitations

- **No video detection, no Accessibility Service, no intelligence** — the
  badge is a static "AI" text. Sprint 10.2 owns detection; 10.3+ owns the
  real overlay design.
- **The overlay does NOT restart automatically** when the app is reopened or
  the process is killed (`START_NOT_STICKY` + explicit start only) — a
  deliberate, documented §19 decision for predictable behavior.
- **`POST_NOTIFICATIONS` is declared but not requested at runtime** (Sprint
  10.1 scope): on Android 13+ the foreground-service notification may stay
  out of the notification *shade* until granted; the task-manager entry and
  the overlay itself are unaffected.
- **Device behavior is untested from this environment** — §12 is a manual
  checklist for a physical device; nothing in this document claims it passed.
- The **debug APK is large** (unstripped debug engine JARs are part of every
  Flutter debug build); release signing is the Flutter default debug key.
- Emulator-only note: the badge draws over *any* app (launcher included) —
  that is exactly what "above YouTube" requires; no app is targeted by name.
- Java 25 + Gradle 8.14 is an upstream incompatibility; when the project
  moves to Gradle 9.x the JBR restriction disappears (documented, not
  worked around in project files).

## 12. Manual device test checklist (§30 — requires a physical device)

Cannot be automated from this environment; run on a real phone:

1. Install the APK, launch → setup screen shows "○ Required".
2. Tap **Enable Overlay** → system "Display over other apps" screen →
   grant → back → screen now shows "✓ Enabled" without app restart.
3. Tap **Start AI Overlay** → notification appears (silent) → small "AI"
   badge shows above the YouTube app.
4. Watch a video, scroll, open comments — touches **outside** the badge
   behave normally (pass-through), tapping the badge shows the toast.
5. Tap **Stop AI Overlay** → badge disappears, state `DISABLED`; tapping
   stop again does nothing harmful.
6. Start, then revoke the permission from system settings → return to the
   app → service stopped, state `PERMISSION_REQUIRED`.
7. Background/foreground the app repeatedly → state and permission stay
   correct (no flicker, no duplicate badge).
8. Swipe the app away from recents while active → service/badge are gone
   and **do not come back by themselves**; reopening the app shows the
   honest state; only an explicit start recreates the badge.
9. Tap Start/Stop rapidly (duplicate taps) → exactly one badge, clean stop.
10. `adb logcat -s sentiment-ai` shows only the §28 vocabulary events, no
    user data.

## 13. Not in this sprint (Sprint 10.2+)

Accessibility Service · YouTube tree parsing · video/URL/ID detection ·
comment acquisition from mobile · intelligence/analytics calls · realtime ·
the real AI Orb / Peek / Expanded overlay design · mobile navigation ·
release signing/Play packaging.
