# Data Flow — The Big Data Pipeline

The Database / Big Data lifecycle is a first-class architectural artifact.
Each stage has a single responsibility and a defined failure behavior.

```text
COLLECT
   ↓
INGEST
   ↓
VALIDATE
   ↓
CLEAN
   ↓
NORMALIZE
   ↓
STORE
   ↓
PROCESS
   ↓
ANALYZE
   ↓
AGGREGATE
   ↓
CACHE
   ↓
SERVE
   ↓
VISUALIZE
```

## End-to-End Flow (with actors)

```text
Chrome Extension                Backend API                 Pipeline / AI
──────────────                  ───────────                 ─────────────
detect videoId
   │ POST analyze(videoId)
   ├──────────────────────►  validate + cache check
   │                              │ cache hit ──► return stored analysis ──► overlay
   │                              │ cache miss
   │                              ▼
   │                         create AnalysisJob ──► queue/worker (async)
   │ GET job status ◄─────────────┤                        │
   │                              │                        ▼
   │                              │                 COLLECT: YouTube Data API
   │                              │                   videos.list (1 unit)
   │                              │                   commentThreads.list paged (1 unit/page)
   │                              │                        ▼
   │                              │                 INGEST → VALIDATE → CLEAN → NORMALIZE
   │                              │                        ▼
   │                              │                 STORE (raw/processed as justified)
   │                              │                        ▼
   │                              │                 PROCESS + ANALYZE (6 AI layers)
   │                              │                        ▼
   │                              │                 AGGREGATE → STORE results
   │                              │                        ▼
   │                              │                 CACHE by videoId
   │ GET analysis(videoId) ◄──────┴────────────────────────┘
   ▼
overlay renders results (or honest failure state)
```

## Stage Definitions

### 1. COLLECTION
Acquire permitted YouTube metadata and comments through appropriate APIs.
- **Uses:** official YouTube Data API v3 only — `videos.list` (metadata/statistics)
  and `commentThreads.list` (paged threads, `nextPageToken`).
- **Constraints:** default quota 10,000 units/day/project; comment paging must be
  bounded (configurable page cap) to fit quota and latency budgets.
- **Failure modes:** quota exhausted, comments disabled, video unavailable,
  network errors → job failed with categorized reason.

### 2. INGESTION
Move retrieved data into the backend processing pipeline in a structured, replayable
way (job-scoped batches). Nothing enters storage unvalidated downstream.

### 3. VALIDATION
Verify required fields, video identifiers, timestamps, response structure, and data
integrity. Malformed payloads are rejected and logged — never partially processed
silently.

### 4. CLEANING
Handle: duplicate comments, empty comments, malformed text, excessive whitespace,
URLs, mentions, emojis (preserved when sentiment-relevant), spam-like content,
unsupported languages. Cleaning decisions are documented and reversible in testing
(raw data retained only as long as justified).

### 5. NORMALIZATION
Prepare text into a consistent representation for downstream NLP (casing policy,
whitespace, unicode handling, language tagging, tokenization-ready form).

### 6. STORAGE
Store raw and/or processed data **only where justified**. Draft entities and
indexing needs: [system-architecture.md](system-architecture.md#candidate-data-entities-not-finalized).
Retention and embed-vs-reference decisions happen in the database sprint (TDR).

### 7. PROCESSING
Run NLP/ML operations over normalized comments (per-comment inference).

### 8. ANALYSIS
Produce the structured six-layer output (sentiment, emotion, topics, aspects, key
opinions, grounded summary) — see [ai-analysis-spec.md](ai-analysis-spec.md).

### 9. AGGREGATION
Convert individual comment-level predictions into meaningful statistics and
patterns: distributions, percentages, confidence, topic rankings, aspect→sentiment
pairs, representative evidence.

### 10. CACHING
Store finished analyses keyed by video ID to avoid unnecessary repeated processing,
API quota spend, and latency. Invalidation policy defined when cache tech is chosen.

### 11. SERVING
Expose structured results through the API (status + result endpoints), validated and
platform-agnostic.

### 12. VISUALIZATION
Render the intelligence through the Chrome overlay: sentiment, emotions, topics,
aspects, insights — in the futuristic HUD style. Error/empty states render honestly;
temporary values are labeled `DEMO DATA`.

## Data Transmitted Between Components

| From → To | Data |
|---|---|
| Extension → Backend | video ID, page kind, request metadata (no user identity) |
| Backend → Extension | job status; structured analysis results; error codes |
| Backend → YouTube API | API key (header), video ID, paging tokens |
| YouTube API → Backend | public video metadata; public comment threads |
| Pipeline → DB | raw/normalized comments (as justified), jobs, results |
| DB → Cache | finished analysis payloads keyed by videoId |

## Confidentiality Notes

- No end-user personal data is collected at any stage.
- Comment text is public content analyzed in aggregate; persistence/retention
  finalized in the data sprint and disclosed in
  [../06-non-functional-requirements.md](../06-non-functional-requirements.md#privacy-data-minimization).
