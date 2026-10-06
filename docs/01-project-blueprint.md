# 01 — Project Blueprint

## Project Identity

| Field | Value |
|---|---|
| Domain | Database / Big Data |
| Official Project Title | Sentiment Analysis of Social Media Data |
| Product Name | *To be finalized* |
| Primary Platform | YouTube |
| Initial Client | Chrome / Chromium Browser Extension (Manifest V3) |
| Future Platform | Android — **not** part of the current implementation |

## Product Vision

> **Transform overwhelming social-media feedback into an understandable layer of
> AI-generated intelligence without forcing users to leave the platform they are
> already using.**

The user opens YouTube normally. A small AI entry point floats on the page. When
activated, the system analyzes the currently relevant video and displays the
resulting intelligence **directly over the YouTube experience**.

Instead of:

1. Copying a YouTube URL,
2. Opening another website,
3. Pasting the URL,
4. Waiting for a separate dashboard,
5. Manually inspecting thousands of comments —

the flow is:

```text
YouTube
   ↓
Current Video
   ↓
AI Analyzer
   ↓
Comment/Data Acquisition
   ↓
Big Data Processing
   ↓
AI/NLP Analysis
   ↓
Sentiment + Emotion + Topics + Aspects
   ↓
Aggregated Intelligence
   ↓
Transparent AI Overlay
```

The product should feel like an **intelligence layer attached to YouTube**, not a
website with a YouTube feature.

## Product Philosophy

The six principles in [07-product-principles.md](07-product-principles.md) govern all
future sprints:

1. **Intelligence layer, not another website** — the experience lives where the content lives.
2. **Complex backend, simple experience** — massive processing underneath, simple numbers above.
3. **Explain the sentiment** — never stop at "Positive: 72%"; answer *why*.
4. **Evidence before AI summary** — AI output must be grounded in actual analyzed data.
5. **Dynamic intelligence** — analysis must track the *currently active* video; never show stale results.
6. **Real data only** — production results come from real permitted sources; anything temporary is marked `DEMO DATA`.

## What We Are Building

A system with two halves:

### Client — Chrome Extension (presentation only)
- Detects YouTube pages and the active video.
- Provides the AI activation point (floating button).
- Renders a transparent futuristic overlay with analysis results.
- Handles SPA navigation so results always match the current video.
- Contains **no** API keys, **no** comment retrieval, **no** AI logic.

### Backend — Data + Intelligence (all heavy lifting)
- Retrieves permitted YouTube metadata and comments.
- Runs the Big Data lifecycle: ingest → validate → clean → normalize → store →
  process → analyze → aggregate → cache → serve.
- Runs the AI/NLP engine: sentiment, emotion, topics, aspects, key opinions,
  evidence-grounded summary.
- Serves a structured analysis API consumed by the extension.

## Why This Is a Database / Big Data Problem

A single popular video can generate comment text far beyond practical manual reading.
The system is a **scalable pipeline for increasing volumes of unstructured
social-media comments**: ingestion, processing, storage, indexing, aggregation,
caching, asynchronous processing, and measured performance are first-class
architectural concerns — not afterthoughts. See
[architecture/data-flow.md](architecture/data-flow.md).

Scalability is defined as *efficiently processing increasing volumes of unstructured
comments* — not as an arbitrary "millions of records" claim
(see [10-success-criteria.md](10-success-criteria.md) for how it will actually be measured).

## Why YouTube First

- One platform with a **verified, official data API** (YouTube Data API v3) exposing
  metadata and comment threads with predictable pagination and quota.
- A single, uniform page structure makes reliable video-context detection feasible.
- Enormous volumes of unstructured comment data — a genuine Big Data source.
- The overlay experience is native to the platform: results appear where the
  conversation already is.

Other platforms (Instagram, Reddit, X, TikTok) and Android remain future scope.

## Why This Differs From a Conventional Sentiment Website

| Conventional website | This product |
|---|---|
| User leaves YouTube, pastes URLs | User never leaves YouTube |
| Static dashboard pages | Transparent overlay layered on the live page |
| One-shot forms | Tracks SPA navigation automatically |
| Generic charts | HUD-style, video-specific intelligence |
| Analysis detached from context | Bound to the *currently playing* video |

## Design Direction

The overlay communicates: futuristic, premium, transparent, intelligent, minimal,
cinematic, technological, unobtrusive — built from **glass + transparency + subtle
glow + thin borders + soft blur + HUD-inspired information + controlled animation**.
It must never become a conventional dashboard, and the YouTube experience must stay
visible underneath. Detailed visual rules live in
[architecture/chrome-extension-architecture.md](architecture/chrome-extension-architecture.md).
