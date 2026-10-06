# Overlay Design — AI Analyzer UI (Sprint 1)

## Design Language

Futuristic, premium, transparent, intelligent, minimal, cinematic,
technological, unobtrusive — built from:

```text
Glass + Transparency + Backdrop blur + Subtle glow + Thin borders
+ Soft shadows + HUD-inspired information + controlled animation
```

**Avoided:** excessive neon, giant gradients, gaming clutter, particle effects,
unnecessary 3D, heavy animation, anything that obstructs the YouTube video.
YouTube remains the primary visual background — the panel is a translucent
glass layer (`linear-gradient` dark navy at 68–84% alpha + `backdrop-filter:
blur(20px) saturate(150%)` + 1px `rgba(151,176,255,.18)` border).

**Color tokens (as implemented):**

| Token | Value | Use |
|---|---|---|
| Glass base | `rgba(15,20,34,.84) → rgba(8,11,20,.68)` | Panel background |
| Text primary | `#e8eefc` | Body copy |
| Text muted | slate-300/400 on dark | Secondary copy (≥ 4.5:1) |
| HUD label | `#9db3d9` | Uppercase micro-headings |
| Accent | indigo `rgba(99,102,241,…)` → `rgba(56,89,214,…)` | Analyze button, FAB core |
| Focus ring | `#9ec1ff` | All keyboard focus |
| State colors | green READY / violet OPEN / blue ANALYZING / red ERROR (see chip table in `overlay.css`) | Status chip |

## FAB — AI Entry Point

```text
        (fixed, right 24px, bottom 96px)
                     ┌───────┐
                     │   ✦   │   58×58 circle
                     │  AI   │   indigo radial gradient,
                     └───────┘   soft blue glow, 1px light border
```

- Position avoids the player control bar (bottom of player) and YouTube's
  corner controls; stays visible while scrolling; survives SPA navigation
  (host is never unmounted).
- **Feedback:** hover → lift + brighten + stronger glow; active → press-scale
  (0.96); keyboard focus → 2px `#9ec1ff` outline; entrance → one-time
  scale/fade animation.
- `aria-haspopup="dialog"`, `aria-label="Open AI Analyzer"`, `title` tooltip.
- Deliberately not a generic material/bootstrap button: custom gradient core,
  glow ring, stacked ✦/AI glyph composition.

## Overlay Panel

```text
┌──────────────────────────────────────┐
│ ✦  AI ANALYZER   [● READY]     – ✕  │  header: identity + state chip + controls
├──────────────────────────────────────┤
│ (notice banner, aria-live)           │  transient messages
│                                      │
│  CURRENT VIDEO                       │  HUD section
│  ┌ ID        dQw4w9WgXcQ ┐           │
│  │ Page      watch       │           │
│  │ Status    READY       │           │
│  └───────────────────────┘           │
│  ┌ AUDIENCE INTELLIGENCE ┐           │
│  │ This intelligence layer will …    │  honest shell copy (§27 of brief)
│  │ Analysis services will be …       │
│  │        [      Analyze      ]      │
│  └───────────────────────┘           │
├──────────────────────────────────────┤
│ SENTIMENT AI                  SPRINT 1│ footer micro-HUD
└──────────────────────────────────────┘
```

- Geometry: 380×min(640, 100vh−96), `top:72 right:16`, rounded-2xl; header 44px;
  body scrolls (`scrollbar-width: thin`, glow-blue thumb); footer 28px.
- **Header:** ✦ + `AI ANALYZER` (11px, .22em tracking), status chip, minimize
  & close icon buttons (28×28, hover bg, labeled).
- **Status chip:** pill with pulsing dot (`box-shadow` glow), color per state:
  `closed gray · open violet · ready green · loading/analyzing blue · complete
  green · error red`.

## State Model (UI mapping)

```text
CLOSED ──FAB──▶ OPEN ──mount──▶ READY ──Analyze──▶ ANALYZING ──stub──▶ READY (+notice)
                                  ▲                                  │
                                  └──────── error path ◀─────────────┘
future: LOADING / COMPLETE / ERROR already modeled in OverlayStatus
MINIMIZE ⇄ full panel   (orthogonal flag; component stays mounted; state kept)
```

Body views rendered by `OverlayBody`:

| Condition | View |
|---|---|
| `status === 'error'` | ERROR + friendly message + Retry |
| `analyzing` | Spinner + "Initializing analysis…" (explicit: no results generated yet) |
| no context | "AI Analyzer could not initialize. Please reload the page." |
| not YouTube (defense) | "NOT AVAILABLE — Available on supported YouTube pages." |
| unsupported page | "NOT AVAILABLE — This page is not currently supported." + hint "Open a supported YouTube video…" |
| video page, no valid id | "NOT AVAILABLE — Unable to identify the current video." |
| ready | Current video card + honest audience-intelligence shell + Analyze |

**No sentiment numbers exist anywhere.** Any future placeholder values must
carry a visible `DEMO DATA` marker (Sprint 0 Principle 6).

## Minimize Behavior

```text
FULL OVERLAY ── [–] ──▶ COMPACT PILL:  ✦ AI  [● READY]  [⤢] [✕]
      ▲                        │
      └────── [⤢] ─────────────┘
```

- Same component instance — `minimized` only hides body/notice/footer and
  shrinks the panel (`w-auto rounded-full`); `status`, `videoContext`, and
  `notice` are untouched (verified by test).
- Focus moves to the restore button when minimized; restore returns focus to
  the panel.

## Motion

| Moment | Animation | Duration |
|---|---|---|
| FAB entrance | scale 0.6→1 + fade (one-time) | 420ms spring-ish ease |
| Panel open | translateY(10px)→0 + fade (one-time) | 260ms |
| FAB hover/active | transform + glow transitions | 180ms |
| Spinner | rotate (only while analyzing) | 800ms/turn |

No idle/continuous loops. `prefers-reduced-motion: reduce` kills all of the
above (0.01ms), leaving static states.

## Accessibility Summary

- All controls are real `<button>`s with accessible names; dialog role with
  label; polite live region for notices; `aria-busy` during analysis.
- Focus lifecycle: FAB → panel (open) → controls (Tab) ; minimize → restore
  button ; close → FAB. Escape closes.
- Contrast, focus visibility, reduced motion as documented in
  `docs/architecture/chrome-extension-architecture.md`.

## Responsive

Verified geometry at 1280×720 / 1366×768 / 1440×900 / 1920×1080 plus short
viewports (800×500 / 1024×600) and a narrow one (400×700): panel height
clamps to `100vh − 96px`, width clamps to `100% − 2rem` (containing block —
not `100vw`, which counts the classic scrollbar), so no overflow at any
size; FAB offset constant. Clamps must stay in **px/%, never rem**: the host
page sets the root font-size (YouTube uses 10px, so `6rem` was only 60px and
clipped the panel bottom by 12px — fixed in the px-lock pass).
(Detailed results in `docs/development/sprint-1.md`.)
