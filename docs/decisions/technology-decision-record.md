# Technology Decision Record (TDR)

Central register of technology choices. Each entry follows:

1. Why it is needed · 2. Alternatives · 3. Advantages · 4. Limitations ·
5. Project fit · 6. Deployment implications · 7. Maintenance implications.

**Sprint 0 status:** candidates evaluated on paper; decisions marked
`DECIDED` are binding, `CANDIDATE` entries are provisional and finalized in their
dedicated sprint with measurements. No technology is selected merely because it
is popular (Sprint 0 Rule 6 — no over-engineering).

---

## TDR-01 — Extension Platform

**Status: DECIDED — Chrome/Chromium Manifest V3**

1. *Needed:* deliver the overlay inside YouTube.
2. *Alternatives:* MV3 (current requirement), MV2 (deprecated), bookmarklet
   (no isolation/persistence), userscript manager (weaker distribution/trust), embedded web widget on youtube.com (not permitted / not our page).
3. *Advantages:* first-class page integration, current Chrome mandate, isolated content-script world, store distribution path.
4. *Limitations:* MV3 constraints (no persistent background unless needed); subject to Chrome policy review.
5. *Fit:* exactly matches the Chrome-first scope decision.
6. *Deployment:* load-unpacked for development; packaged build later.
7. *Maintenance:* follow Chrome release cadence; manifest reviewed each sprint.

## TDR-02 — Client Language & UI Framework

**Status: DECIDED — TypeScript + React (with Tailwind CSS where appropriate)**

1. *Needed:* maintainable, typed UI with a rich state model (many overlay states).
2. *Alternatives: Svelte, Vue, vanilla TS + Web Components, HTML/CSS only.
3. *Advantages:* TS type safety across services/UI; React's declarative state→UI
   mapping fits the overlay state machine; huge ecosystem; Tailwind speeds
   consistent HUD styling.
4. *Limitations:* bundle weight vs vanilla (kept in check: single content bundle,
   no unnecessary dependencies); Tailwind must respect Shadow-DOM isolation.
5. *Fit:* preferred stack in the project brief; team tooling standardization.
6. *Deployment:* bundled at build time — no remote code (MV3 compliance).
7. *Maintenance:* conventional, well-documented stack.

## TDR-03 — Client Build Tooling

**Status: DECIDED — Vite (Sprint 1, implemented & verified)**

Selection criteria met: single-bundle IIFE content-script output, TS/JSX
support, Tailwind v4 integration (`?inline` CSS → Shadow DOM), minimal config,
`vite build` green in CI-style `npm run verify`. Alternatives (esbuild-only,
webpack, rollup-manual) offered no advantage at this scale.

## TDR-04 — Backend Framework

**Status: DECIDED — Python + FastAPI + Pydantic (implemented in Sprint 2)**

1. *Needed:* API for video-id validation, acquisition requests, result serving.
2. *Alternatives: Django (REST), Flask, Node/Express, Go services.
3. *Advantages:* async-ready (fits future job orchestration), pydantic validation
   as the external-data boundary, one language with the future AI pipeline,
   automatic OpenAPI docs.
4. *Limitations:* Python throughput vs compiled languages (adequate at MVP
   volume; sync httpx client in the threadpool for now).
5. *Fit:* AI layer is Python-based regardless → shared libraries/types.
6. *Deployment:* venv/`requirements.txt` today; containerization later; env-var
   secrets (`.env`, git-ignored).
7. *Maintenance:* mature frameworks; 21-test suite guards the contract.

*Implementation evidence:* `backend/app/` + live-verified `/health`, error
mapping, CORS behavior (see docs/13-backend-api-contract.md).

## TDR-05 — Data Source (External API)

**Status: DECIDED — YouTube Data API v3, backend-only**

1. *Needed:* permitted access to video metadata and comments (FR-05).
2. *Alternatives: scraping (ToS risk — rejected), unofficial APIs (unstable/rejected), third-party comment providers (external dependency risk).
3. *Advantages:* official, documented, stable contract; paged comment retrieval.
4. *Verified facts (researched Sprint 0):*
   - `commentThreads.list` quota cost **1 unit/call**, paged via `nextPageToken` (≤100 threads/page).
   - `videos.list` quota cost **1 unit/call**.
   - Default quota **10,000 units/day per Google Cloud project**, resets midnight Pacific;
     `search.list` costs 100 units (avoidable in MVP design).
   - Comment collection must therefore be **page-capped and cached** — a direct
     Big Data design constraint (quota-aware ingestion).
   - Videos with comments disabled error/return no items → graceful failure state
     (exact error shape verified during API integration — Rule 3: research before coding).
5. *Fit:* satisfies FR-05, real-data principle.
6. *Deployment:* API key stored server-side only (secret manager/env), quota monitored.
7. *Maintenance:* re-verify quota/pricing facts each sprint; quota exhaustion is a
   designed failure state.

## TDR-06 — Database

**Status: DECIDED (Sprint 3) — SQLite (stdlib `sqlite3`) via a repository
layer; PostgreSQL is the documented migration target.**

Full justification, requirement analysis, alternatives matrix (MongoDB,
PostgreSQL, MySQL, SQLAlchemy, DuckDB), indexing rationale, scalability and
migration path: [`docs/architecture/database.md`](../architecture/database.md).

Decision summary:

- **Chosen:** SQLite file store (`DATABASE_URL=sqlite:///./data/sentiment.db`,
  `sqlite:///:memory:` in tests). All SQL is confined to
  `backend/app/db/repository.py` — the seam for the Postgres move.
- **Why not MongoDB/PostgreSQL *now*:** both assume a running server this
  project does not deploy. The measured workload (per-video datasets, atomic
  dedup on the unique `comment_id`, SQL-side aggregates, single-process
  FastAPI) is a textbook embedded-relational fit; SQLite adds zero runtime
  dependencies.
- **Verified in code:** dedup via `INSERT … ON CONFLICT` (same comment twice
  ⇒ one row), video-level isolation via FK + filtered reads, indexes on
  `(video_id, published_at)` and `(video_id, processing_status)` — covered by
  the Sprint 3 test suite.

The rule was honoured: not selected for popularity, but for fit — the old
evaluation matrix is folded into `database.md` with verdicts.

*Old matrix (Sprint 2 draft), retained for the record:*

| Criterion | MongoDB | PostgreSQL | Notes |
|---|---|---|---|
| Comment volume handling | | | large per-video comment sets |
| Schema flexibility (messy comment JSON) | native documents | JSONB | raw payloads vary |
| Indexing (videoId, status, time) | | | lookup patterns from entities draft |
| Aggregation (topic/aspect stats) | aggregation pipeline | SQL/window functions | pre-aggregation likely favors one side |
| Scalability | horizontal via sharding | vertical + read replicas | MVP scale is modest — do not over-engineer |
| Caching synergy | TTL indexes possible | via companion cache | TTL could double as analysis expiry |
| Query needs | ad-hoc analysis queries | strong relational integrity | job/state relations |
| Dev/deploy complexity | low | low–medium | team familiarity |

**Rule:** do not blindly select; document the winner here as `DECIDED` with
justification, retention policy, and the final schema reference.
→ **Fulfilled:** verdict + final schema now live in
`docs/architecture/database.md` + `docs/architecture/data-model.md`.

## TDR-07 — Cache & Job Processing

**Status: CANDIDATE — requirement is asynchronous processing + caching, NOT a named tool.**

Candidates (to evaluate against actual need): in-process/DB-backed cache, Redis,
Celery/RQ, RabbitMQ, BullMQ (Node — only relevant if backend language changes).
MVP-scale default hypothesis to *test first*: simplest option that satisfies FR-09
and the async flow (possibly DB-backed queue + TTL cache), upgrading only if
measurements demand it. Decision recorded here in the architecture sprint.

*Sprint 2 note:* the cache **seam** now exists and is exercised - an in-process
TTL + LRU cache (`backend/app/services/cache.py`, default 600s/128 entries)
fronting the YouTube client. It is explicitly a development boundary, not the
final production cache. *Sprint 3 note:* TDR-06 is decided (SQLite dataset
store + freshness TTL became the second cache layer, see
`docs/architecture/caching.md`); **TDR-07 (async jobs / Redis-class cache)
remains open.**

## TDR-08 — AI/ML Toolchain

**Status: CANDIDATE — Transformers/PyTorch, scikit-learn, spaCy, NLTK (+ optional LLM APIs).**

Not selected in Sprint 0 (see the model-selection rule in
[../architecture/ai-analysis-spec.md](../architecture/ai-analysis-spec.md)).
Evaluation axes: F1 on labeled sample, inference cost at expected comment volume,
language coverage, deployment footprint. Baseline-first methodology mandatory.

---

## Decision Log Summary

| TDR | Topic | Status | Decide by |
|---|---|---|---|
| 01 | Extension platform MV3 | DECIDED | — |
| 02 | TypeScript + React (+Tailwind) | DECIDED | — |
| 03 | Build tooling | DECIDED (Vite) | Sprint 1 ✅ |
| 04 | Backend framework | DECIDED (FastAPI) | Sprint 2 ✅ |
| 05 | YouTube Data API v3 (backend-only) | DECIDED | — |
| 06 | Database | CANDIDATE | Database sprint |
| 07 | Cache / job mechanism | CANDIDATE (seam exists) | Architecture sprint |
| 08 | AI/ML toolchain & model | CANDIDATE | AI/ML sprint |
