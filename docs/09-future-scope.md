# 09 — Future Scope

Everything in this document is **intentionally postponed**. None of it may be
implemented during the initial Chrome MVP. Items are grouped for later roadmap use.

## Platforms

| Platform | Status |
|---|---|
| Android (app + overlay + permissions) | Future — backend/API kept platform-reusable for it |
| iOS | Future |
| Instagram | Future |
| Reddit | Future |
| X (Twitter) | Future |
| TikTok | Future |
| Other social platforms | Future |

## Intelligence Capabilities

| Capability | Status |
|---|---|
| Cross-video comparison | Future |
| Historical sentiment tracking over time | Future |
| Creator analytics | Future |
| Brand / product sentiment portfolios | Future |
| Trend detection | Future |
| Multilingual sentiment (beyond model's native coverage) | Future |
| Voice explanations | Future |
| Real-time streaming analysis | Future |
| Personalized insights | Future |

## Product / UX

| Item | Status |
|---|---|
| Standalone dashboard or website product | Out of scope (not primary experience) |
| User accounts & authentication for consumers | Not in MVP |
| Social / sharing / gamification features | Not in MVP |
| Mobile-responsive web layouts | Future (if ever) |

## Explicit Guardrails

- Future-scope items must not leak into MVP acceptance criteria.
- Designing the **API platform-agnostic** (no Chrome-specific assumptions in the
  backend contract) is *in scope now* precisely so Android later needs no backend
  rewrite — but no Android code, UI, or permissions are written in this phase.
- Candidates for post-MVP selection (cache tech, job queue, model families) live in
  [decisions/technology-decision-record.md](decisions/technology-decision-record.md),
  not in the MVP plan.
