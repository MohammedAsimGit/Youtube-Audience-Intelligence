# 03 — Objectives and Scope

## Sprint 0 Objective

Sprint 0 is the **Foundation & Architecture Sprint**: completely define the product,
technical architecture, data architecture, AI architecture, UX, requirements,
constraints, MVP, and development roadmap **before** production implementation begins.
Sprint 0 produces documentation only — no production feature code.

## Scope Decision: Desktop-First, Chrome-First

The initial implementation is **desktop-first and Chrome-first**. The system must **not**
be designed as a traditional standalone website. The primary experience must happen
directly inside YouTube through a Chrome/Chromium extension.

```text
                    YOUTUBE
                       │
                       ▼
              CHROME EXTENSION
                       │
                       ▼
              FUTURISTIC OVERLAY
                       │
                       ▼
                 BACKEND API
                       │
            ┌──────────┴──────────┐
            ▼                     ▼
       DATA PIPELINE          AI ENGINE
            │                     │
            └──────────┬──────────┘
                       ▼
                ANALYSIS DATA
                       │
                       ▼
                 CHROME OVERLAY
```

## Android: Future Scope Only

Android is **future scope**. During the current project phase do NOT implement:

- Android application or overlay
- mobile UI or mobile-specific architecture
- Android permissions

The backend and analysis layer should be **reusable by Android later**, which is a
design constraint now (keep the client dumb and the API platform-agnostic) — but
Sprint 0 and the MVP remain focused on Chrome Desktop.

## Objectives (What Success Looks Like)

1. A user understands audience reaction to a video **without reading comments**.
2. The user never leaves YouTube to get that understanding.
3. All displayed intelligence is **real, evidence-grounded, and current-video-bound**.
4. The backend demonstrably scales with increasing comment volume (measured, not claimed).
5. Every external dependency has a designed failure state.

## In Scope for the Overall Project

- Chrome/Chromium Manifest V3 extension (desktop).
- YouTube page & video detection, including SPA navigation.
- AI floating action button + transparent futuristic overlay.
- Backend API (candidate: Python + FastAPI).
- YouTube metadata & comment acquisition via the **official YouTube Data API v3**,
  executed **only on the backend**.
- Big Data pipeline: ingest → validate → clean → normalize → store → process →
  analyze → aggregate → cache → serve.
- AI/NLP analysis layers: sentiment, emotion, topics, aspects, key opinions,
  evidence-grounded summary.
- Asynchronous job processing and result caching.
- Evaluation across AI, system, UI, and user dimensions.

## Explicitly Out of Scope

- Android / iOS / any mobile implementation.
- Other social platforms (Instagram, Reddit, X, TikTok).
- Standalone analytics website or dashboard product.
- User accounts, social networking, gamification.
- Any production code during Sprint 0.

See [08-mvp-definition.md](08-mvp-definition.md) and [09-future-scope.md](09-future-scope.md)
for the precise MVP / future split.

## Scope Guardrails (Sprint 0 Rules That Bound Scope)

| Rule | Effect on scope |
|---|---|
| No production feature code in Sprint 0 | Work is documentation/analysis only. |
| Do not invent APIs | External APIs researched & verified before implementation. |
| No fake production intelligence | Fabricated analysis is forbidden; temporary UI data must be marked `DEMO DATA`. |
| Keep Big Data central | Architecture must visibly demonstrate ingestion, processing, storage, indexing, aggregation, scalability, caching, async processing, performance. |
| Do not over-engineer | Every architectural component needs a documented reason. |
| Chrome first | No mobile work in this cycle. |
| Separate client & intelligence | Extension = presentation; backend = data + AI. |
| Design for failure | Every external dependency gets a failure state. |
