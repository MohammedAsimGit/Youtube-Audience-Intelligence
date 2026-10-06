# System Architecture

Sprint 0 blueprint. **Nothing in this document is implemented during Sprint 0.**

## Layer Diagram

```text
                         YOUTUBE
                            │
                            ▼
                 ┌────────────────────┐
                 │ Chrome Extension   │
                 │                    │
                 │ Content Script     │
                 │ YouTube Detection  │
                 │ Overlay UI         │
                 └─────────┬──────────┘
                           │
                     Video Context
                           │
                           ▼
                 ┌────────────────────┐
                 │    Backend API     │
                 └─────────┬──────────┘
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
        Data Layer     Job System     Cache
             │             │             │
             └─────────────┼─────────────┘
                           ▼
                 ┌────────────────────┐
                 │ Data Processing    │
                 │ Validation         │
                 │ Cleaning           │
                 │ Normalization      │
                 └─────────┬──────────┘
                           │
                           ▼
                 ┌────────────────────┐
                 │ AI / NLP Engine    │
                 │ Sentiment          │
                 │ Emotion            │
                 │ Topics             │
                 │ Aspects            │
                 └─────────┬──────────┘
                           │
                           ▼
                 ┌────────────────────┐
                 │ Analysis Storage   │
                 │ + Cache            │
                 └─────────┬──────────┘
                           │
                     Analysis API
                           │
                           ▼
                 ┌────────────────────┐
                 │ Chrome Overlay     │
                 │ Visual Intelligence│
                 └────────────────────┘
```

## Layer Responsibilities

| Layer | Responsibility | Explicitly NOT responsible for |
|---|---|---|
| **Chrome Extension (content script)** | Detect YouTube pages & active video; SPA navigation detection; mount isolated overlay UI; request analysis; render states & errors | Comment retrieval, AI, API keys, storage |
| **Overlay UI (React in extension)** | FAB, overlay shell, state model (ready/loading/analyzing/complete/error), minimize, accessibility | Business logic, direct platform-API calls |
| **Backend API** | Platform-agnostic contract: request analysis, query status, fetch results; validate inputs; rate limiting | Rendering; holding UI concerns |
| **Data Layer** | Persist raw/processed data and analysis results; indexing by video/status/time | AI inference |
| **Job System** | Create & schedule analysis jobs; workers execute pipelines asynchronously; retries & failure states | What the pipeline computes |
| **Cache** | Fast lookup of finished analyses by video ID; reduce API quota & recompute | Source of truth (DB remains source of truth) |
| **Data Processing** | Validation → cleaning → normalization of comment text | Fetching from YouTube (acquisition stage) |
| **AI / NLP Engine** | Six analysis layers producing structured outputs + grounded summary | Inventing data; fetching comments |
| **Analysis Storage + Cache** | Store structured results; serve them to the API | Client concerns |

## Asynchronous Analysis Architecture

Long-running processing must never block the browser interface.

```text
Chrome
  ↓
Request Analysis
  ↓
Backend
  ↓
Create Analysis Job
  ↓
Queue / Worker
  ↓
Data Acquisition
  ↓
Processing
  ↓
AI Analysis
  ↓
Store Result
  ↓
Chrome retrieves analysis
```

**Rule:** the requirement is *asynchronous processing* — not a predetermined
technology. Redis / Kafka / Celery / RabbitMQ / BullMQ are **candidates only**;
the architecture sprint selects (or deliberately rejects) them based on measured
need. Over-engineering is prohibited (Sprint 0 Rule 6).

Job lifecycle states (draft): `queued → running → succeeded | failed | timeout`,
with partial-progress visibility so the overlay can show honest intermediate states.

## Dynamic Video Flow (Stale-Result Prevention)

```text
Video A → Detect A → Analyze A → overlay shows A
   ↓ user navigates (YouTube SPA — no page reload)
Video B → Detect B → invalidate A's display state
   → Check cache for B → hit: show B   |  miss: create job for B
   → Overlay updates with B only
```

The extension owns detection; the backend guarantees results are keyed by video ID;
the UI refuses to render results whose video ID ≠ current context.

## Caching Strategy

```text
Video ID
   ↓
Analysis Cache
   ↓
Exists?
 ┌───────┴──────┐
YES              NO
 ↓                ↓
Return         Create Job
 ↓                ↓
Display        Analyze
                  ↓
                Store
```

Caching reduces: repeated API calls, repeated comment retrieval, repeated NLP
inference, unnecessary backend work, and perceived latency. Cache technology is
selected later; a cache failure must degrade gracefully (NFR reliability table).

## Candidate Data Entities (not finalized)

| Entity | Purpose | Important fields (draft) | Indexing needs (draft) | Relationships |
|---|---|---|---|---|
| **Video** | Identity of analyzed video | videoId, title, channelId, publishedAt, stats snapshot | videoId (unique) | 1 → many Comment, 1 → many Analysis |
| **Comment** | Raw/normalized comment text | commentId, videoId, text, normalizedText, likeCount, publishedAt, parentId, language | videoId; publishedAt; text (search) | many → 1 Video |
| **AnalysisJob** | Async job tracking | jobId, videoId, status, requestedAt, startedAt, finishedAt, error | status; videoId; createdAt | many → 1 Video; 1 → 1 Analysis |
| **Analysis** | Finished result bundle | analysisId, videoId, commentsAnalyzed, dataCoverage, analyzedAt, version | videoId + analyzedAt (compound) | 1 → 1 SentimentResult, EmotionResult; 1 → many Topic, Aspect, Insight |
| **SentimentResult** | Distribution | counts/percentages positive/neutral/negative, confidence | (embedded in Analysis likely) | part of Analysis |
| **EmotionResult** | Emotion distribution | per-emotion counts/percentages | — | part of Analysis |
| **Topic** | Discovered topic | label, frequency, representative comment IDs | videoId; frequency | many → 1 Analysis |
| **Aspect** | Aspect-based sentiment | aspect label, sentiment, support counts | videoId | many → 1 Analysis |
| **Insight** | Key opinions + grounded summary | text, supporting evidence refs | — | many → 1 Analysis |

**Sprint 0 rule:** this is a candidate list only — the final schema is designed in
the database sprint and recorded as a Technology Decision Record. Embedding vs.
referencing (e.g., sentiment/emotion inside Analysis) is an explicit open question.

## Scalability Definition

> A scalable pipeline designed to process increasing volumes of unstructured
> social-media comments efficiently.

Not an arbitrary "millions of records" requirement. Evaluation sizes (when
practical): 1,000 / 10,000 / 50,000 / 100,000+ comments — subject to API quota
(default YouTube Data API quota: 10,000 units/day/project; `commentThreads.list`
= 1 unit/call), infrastructure, dataset availability, and project resources.
**Never claim a scale that has not been tested.**

## Cross-Cutting Rules

- **Secrets:** YouTube API key and any service credentials live only server-side.
- **Validation:** every boundary (browser → API, API → YouTube, queue → workers)
  validates its inputs.
- **Observability:** consistent per-layer logging (init, detection, job lifecycle,
  state transitions); no unnecessary user information logged.
- **Failure:** each external dependency (YouTube API, DB, cache, AI model, network)
  has a designed failure state as specified in
  [../06-non-functional-requirements.md](../06-non-functional-requirements.md).
- **Reusability:** API contract has no Chrome-specific assumptions so a future
  Android client can reuse the entire backend unchanged.
