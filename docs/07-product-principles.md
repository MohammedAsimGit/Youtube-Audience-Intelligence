# 07 — Product Principles

These six principles are binding for every future sprint.

## Principle 1 — Intelligence Layer, Not Another Website

The primary experience must exist where the content already exists. The user should
not leave YouTube to understand audience reaction. Any proposal that requires a
standalone dashboard as the *primary* experience violates this principle.

**Consequences:** overlay-first UX; backend is invisible; no URL-copying workflows.

## Principle 2 — Complex Backend, Simple Experience

The backend may perform large-scale data processing, NLP, machine learning,
aggregation, caching, and asynchronous analysis. The user should see only:

- simple numbers,
- understandable charts,
- meaningful topics,
- clear explanations,
- concise AI insights.

> **Complex processing underneath. Extremely simple interaction above.**

**Consequences:** heavy work is server-side; the overlay stays light; API responses
are pre-aggregated for display.

## Principle 3 — Explain the Sentiment

Do not stop at `Positive: 72%`. The system must eventually answer:

- **Why?**
- What are viewers discussing?
- What do viewers appreciate? What do they criticize?
- What emotions are present?
- Which aspects receive positive or negative reactions?

**Consequences:** the AI spec has six layers (sentiment → emotion → topics →
aspects → key opinions → insight); the overlay reserves space for explanations.

## Principle 4 — Evidence Before AI Summary

AI-generated explanations must be grounded in actual analyzed data. The system must
not invent opinions, statistics, trends, comments, engagement values, or audience
reactions.

**Consequences:** summaries are generated *from* stored analysis output; unknowns
are displayed as unknowns; failure states are honest; `DEMO DATA` labeling is
mandatory for anything temporary.

## Principle 5 — Dynamic Intelligence

The analysis must correspond to the **currently active** YouTube video.

```text
Video A → Analysis A → user navigates → Video B → detect B
        → check cache → analysis B (or fresh job) → overlay updates
```

Stale analysis must never be presented as belonging to a new video.

**Consequences:** SPA navigation detection is mandatory; context change resets or
re-binds overlay state; cache is keyed by video ID.

## Principle 6 — Real Data Only

Production analysis must use real permitted data sources. Never use fabricated
production results. Mock data is allowed **only** for:

1. isolated UI development,
2. component development,
3. testing,
4. demos explicitly marked as `DEMO DATA`.

**Consequences:** temporary UI values carry a visible `DEMO DATA` marker and are
isolated behind a replaceable interface; there is no hardcoded "sample analysis"
path reachable in production flows.

---

## Meta-Principles (Sprint 0 Rules)

- **Keep Big Data central** — ingestion, processing, storage, indexing, aggregation,
  scalability, caching, async processing, and performance must be visible in the
  architecture.
- **Do not over-engineer** — every architectural component needs a documented reason;
  no distributed infrastructure for show.
- **Design for failure** — every external dependency has a failure state.
- **Separate client and intelligence** — the extension presents; the backend thinks.
