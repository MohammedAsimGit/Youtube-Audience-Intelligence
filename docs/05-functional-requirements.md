# 05 — Functional Requirements

Requirements are numbered **FR-xx** and referenced from later sprint definitions.
Each requirement is testable. Backend/AI requirements are *design requirements* for
later sprints; Sprint 0 only documents them.

## FR-01 — Platform Operation

The extension shall operate on supported YouTube pages (desktop Chrome/Chromium,
Manifest V3).

**Notes:** Supported page set is defined in the extension architecture sprint; at
minimum `https://www.youtube.com/watch?v=VIDEO_ID`.

## FR-02 — Active Video Identification

The extension shall identify the active YouTube video (video ID) from the live page
URL. Video IDs must always be parsed from the current URL — never hardcoded.

## FR-03 — AI Activation Mechanism

The extension shall provide an AI activation mechanism (floating AI button) that is
visible, clickable, keyboard-accessible, and does not obstruct essential YouTube
controls.

## FR-04 — Overlay Display

The extension shall display analysis results through a transparent futuristic overlay
layered above YouTube, with clear states for ready / loading / analyzing / complete /
error, plus minimize support that preserves UI state.

## FR-05 — Permitted YouTube Data Retrieval

The backend shall retrieve permitted YouTube video metadata and comment data using
the **official YouTube Data API v3** only, with API keys held server-side.

**Verified API facts (researched for Sprint 0):**
- `commentThreads.list` — quota cost **1 unit** per call, returns paged comment
  threads (`nextPageToken` pagination, up to 100 threads per page).
- `videos.list` — quota cost **1 unit** per call (metadata/statistics).
- Default project quota: **10,000 units/day** (resets midnight Pacific).
- Videos with comments disabled fail or return no comment items — must be handled
  as a graceful failure state (exact error shape verified during API integration).

## FR-06 — Comment Data Processing

The backend shall process comment data through the documented pipeline: validate →
clean → normalize → store, handling duplicates, empty/malformed text, URLs,
mentions, emojis, spam-like content, and unsupported languages.

## FR-07 — Structured AI Analysis

The AI layer shall generate structured analysis covering: sentiment (positive /
neutral / negative with counts, percentages, confidence), emotions, topics,
aspect-based sentiment, key opinions, and an evidence-grounded summary.

## FR-08 — Aggregation

The system shall aggregate individual comment-level predictions into meaningful
statistics and patterns (distributions, rankings, aspect-sentiment pairs).

## FR-09 — Caching

The system shall cache reusable analysis keyed by video ID so repeated requests avoid
re-collection, re-inference, and repeated API quota spend.

## FR-10 — YouTube Navigation Handling

The extension shall handle YouTube's SPA navigation: when the user moves from video A
to video B without a page reload, the extension must detect the new video context.

## FR-11 — No Stale Analysis

The system shall prevent stale analysis from being displayed: results for video A
must never be presented as belonging to video B. Overlay state must be reset or
re-bound whenever the video context changes.

## FR-12 — Graceful Failure Handling

The system shall gracefully handle unavailable data and service failures — including
comments disabled, video unavailable/deleted, insufficient comments, API quota
exhaustion, network failure, malformed responses, analysis timeout, and backend/
database/cache outages — by showing honest error/empty states. **The system must
never fabricate results when real analysis cannot be performed.**

## Traceability

| FR | Owner layer | First implementation sprint |
|---|---|---|
| FR-01..04, FR-10 | Chrome extension | Sprint 1 (foundation) → Sprint 5 (full lifecycle) |
| FR-05, FR-06 | Backend data pipeline | Sprints 2–3 |
| FR-07, FR-08 | AI engine | Sprint 4 |
| FR-09 | Backend/cache | Sprint 5 |
| FR-11 | Extension + backend contract | Sprint 1 (mechanism), Sprint 5 (lifecycle) |
| FR-12 | All layers | Designed now, implemented throughout |
