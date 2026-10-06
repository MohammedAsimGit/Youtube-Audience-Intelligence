# 08 — MVP Definition

The MVP is the **Chrome Desktop MVP**: one extension, one platform, one backend,
one AI pipeline — complete enough to deliver real, grounded intelligence in the overlay.

## In the MVP (20 items)

| # | Item | Layer |
|---|---|---|
| 1 | Chrome/Chromium Manifest V3 extension | Client |
| 2 | YouTube page detection | Client |
| 3 | Current video identification | Client |
| 4 | AI floating button | Client |
| 5 | Futuristic transparent overlay | Client |
| 6 | YouTube video metadata retrieval | Backend |
| 7 | Comment retrieval (official YouTube Data API v3) | Backend |
| 8 | Comment preprocessing (validate/clean/normalize) | Pipeline |
| 9 | Database storage (decision via TDR) | Data |
| 10 | Sentiment classification (positive/neutral/negative) | AI |
| 11 | Basic emotion analysis | AI |
| 12 | Topic extraction | AI |
| 13 | Basic aspect analysis (aspect → sentiment) | AI |
| 14 | Evidence-grounded AI summary | AI |
| 15 | Analysis API | Backend |
| 16 | Asynchronous analysis processing (jobs/queue) | Backend |
| 17 | Caching by video ID | Backend |
| 18 | Dynamic video detection (SPA navigation) | Client |
| 19 | Error handling (all failure states from NFR table) | All |
| 20 | Production-quality desktop UX (a11y, responsive, visual polish) | Client |

## Explicitly NOT in the MVP

- Android, iOS, or any mobile application/overlay/permissions
- Instagram, Reddit, X, TikTok, or any platform other than YouTube
- Cross-platform analytics
- Unnecessary user accounts or authentication for end users
- Social networking features
- Excessive gamification
- Unrelated dashboards or a standalone website product
- Real-time streaming analysis
- Historical/trend analytics across time (beyond what a single analysis shows)

## MVP Entry/Exit Behavior (user-visible)

1. User is on a supported YouTube page → small AI button is present.
2. User activates it → transparent overlay opens (state: ready).
3. User requests analysis → overlay enters analyzing state → real backend job runs
   (async) → results render (sentiment, emotions, topics, aspects, summary) **or**
   an honest failure/empty state renders.
4. User navigates to another video (SPA) → context updates; previous video's results
   are never shown for the new video; cache is checked before new work.
5. User can minimize/close the overlay; UI state is preserved where sensible.

## MVP Quality Bar

- Zero unnecessary extension permissions; no secrets in the client.
- No fabricated numbers anywhere in production flows.
- Keyboard-accessible, contrast-safe overlay at standard desktop resolutions.
- Documented, measured evaluation results (see [10-success-criteria.md](10-success-criteria.md)).
