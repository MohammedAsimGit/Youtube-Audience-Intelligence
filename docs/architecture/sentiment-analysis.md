# Sentiment Analysis (Sprint 4)

The first interpretation layer over the stored dataset: every
`READY_FOR_ANALYSIS` comment becomes a persisted, reproducible verdict, and
the extension renders the aggregates as a dark intelligence console.

```text
SQLite (READY_FOR_ANALYSIS)
   ↓  keyset batch reads (comment_batch_size)
SentimentService            backend/app/services/sentiment.py
   ↓  claim → classify → persist (state machine, repository only)
SQLite (PROCESSED / FAILED, sentiment columns)
   ↓  SQL aggregation (GROUP BY sentiment_label)
GET /api/videos/{id}/sentiment
   ↓  camelCase contract
Chrome extension → futuristic overlay
```

The extension **never runs a model**. It only GETs the endpoint above; the
backend owns triggering, execution, persistence, and aggregation.

## Model selection (documented choice)

| | |
|---|---|
| **Library** | `vaderSentiment` ≥ 1.0.0 (VADER, Hutto & Gilbert 2014) |
| **Type** | Rule-based lexicon scoring (no learned weights, no network) |
| **Added to** | `backend/requirements.txt` (Sprint 4) |
| **Runtime** | Pure Python, CPU-only, no GPU, no model download, no API |

Why VADER for this sprint (the inspection-first rule in §6):

- **Deterministic**: identical input ⇒ identical output, run after run
  (`DetectorFactory`-style reproducibility is asserted in tests; the engine
  has no sampling, no weights to load).
- **Environment-safe**: installs and imports in the existing venv alongside
  the Sprint 3 stack; nothing else required.
- **Honest continuous score**: VADER's compound score is a real value in
  `[-1, 1]` on the documented `Negative ← 0 → Positive` scale, so no score
  is invented (§8).
- **Social-text aware**: emoji, capitalization, and punctuation shift the
  score the way they shift real commenters — appropriate for YouTube text.
- **Small**: one lexicon file, no transformers/torch stack (§6: "do not
  silently add a large AI stack").

Rejected for Sprint 4: transformer models (large download, GPU-ish
footprint, non-trivial determinism), cloud LLM APIs (cost, keys, latency,
non-determinism), lexicon alternatives with no compound score (would force a
fabricated score). Revisit under TDR-08 when an evaluation harness exists.

**Engine identity** is persisted per row in `sentiment_model` as
`vader-1.0` — the database records exactly what produced each verdict — but
the **public API never exposes model internals** (§8).

## Supported languages (explicit policy, no translation)

```text
language == 'en'  ──▶ classified
anything else     ──▶ PROCESSED with label UNSUPPORTED_LANGUAGE (reported as `skipped`)
```

- VADER's lexicon is **English**. `SUPPORTED_LANGUAGES = {"en"}` in
  `services/sentiment.py` is the single source of truth.
- The gate uses the `language` column already written by Sprint 3's
  langdetect stage (`seed=0`, confidence ≥ 0.90, else `unknown`).
- `unknown` is **not** analyzable: an undetectable language is not pretend-
  analyzable, so it is skipped like any other unsupported code.
- Skipped rows are **never folded into NEUTRAL** (that would fabricate a
  verdict for text the model cannot read). They surface as
  `stats.skipped`, and a dataset where *everything* was skipped renders the
  honest `INSUFFICIENT LANGUAGE SUPPORT` state in the UI (§27).
- **No automatic translation** in Sprint 4 (§7). Translation is a later
  enhancement and would be documented as its own pipeline stage.

## Input / output

**Input**: one comment's `normalized_text` (the pipeline-cleaned text), with
the row's stored `language` as the gate. The engine function `classify(text)`
is pure: text in → `SentimentVerdict(label, score, confidence)` out, with no
SQLite/FastAPI/YouTube coupling (§4) — the seam a later sprint can swap.

**Output per comment** (persisted through the repository):

| Column | Value |
|---|---|
| `sentiment_label` | `POSITIVE` · `NEUTRAL` · `NEGATIVE` · `UNSUPPORTED_LANGUAGE` · `NULL` (failed / not analyzed) |
| `sentiment_score` | VADER compound in `[-1.0, 1.0]`; `NULL` for skipped/failed |
| `sentiment_confidence` | decision margin in `[0, 1]` (semantics below); `NULL` for skipped/failed |
| `sentiment_model` | `vader-1.0` |
| `sentiment_processed_at` | ISO-8601 UTC of the last attempt |

**Classification thresholds** (VADER's published rule, tested for
self-consistency):

```text
compound ≥ +0.05  →  POSITIVE
compound ≤ -0.05  →  NEGATIVE
otherwise         →  NEUTRAL
```

**Empty text** has no lexical evidence: the engine returns `NEUTRAL`,
score `0.0`, confidence `0.0` — a defined, honest outcome, never a
fabricated verdict. (Ingestion never persists empty text, so this exists
for robustness and is covered by tests.)

## Score & confidence semantics (honest definitions)

```text
score ∈ [-1, 1]:   Negative ◀──── 0 ────▶ Positive   (VADER compound)
confidence ∈ [0, 1]: DISTANCE FROM THE DECISION BOUNDARY — not a probability.
```

- polar labels: `(|score| − 0.05) / 0.95`
- neutral label: `(0.05 − |score|) / 0.05`

A verdict sitting right on its threshold scores ≈ 0 confidence; a verdict
deep inside its band scores ≈ 1. We never present it as "the model is 84%
sure" — it is a decision margin, documented as such (§8: do not invent a
meaningful score the model does not provide; VADER *does* provide the
compound, and the margin is derived transparently from it).

## State machine (existing edges only)

```text
READY_FOR_ANALYSIS ──claim──▶ PROCESSING ──persist──▶ PROCESSED
                                   │
                                   └──persist(failure)──▶ FAILED
FAILED ──requeue (existing edge)──▶ READY_FOR_ANALYSIS
```

All transitions go through `DatasetRepository.update_processing_status`,
`claim_for_processing`, and `save_sentiment_results`, each of which validates
against `models/processing.py` under the database lock — the service
contains **zero SQL** and cannot bypass an edge (an illegal jump raises and
writes nothing).

**Per-comment outcomes** (§13): one bad comment becomes a `FAILED` row with
a structured log event (`SENTIMENT_COMMENT_FAILED`: video id, comment id,
exception class name — **never raw text**). The run continues; 497/500
success is a successful run with 3 recorded failures.

**Orphaned claims**: a crash can leave rows at `PROCESSING`. Because a run
holds the in-process run lock, any `PROCESSING` rows seen when a new run
acquires that lock are orphans (single-process deployment, documented) and
are requeued through the legal path `PROCESSING → FAILED →
READY_FOR_ANALYSIS`, then reprocessed. Rows claimed by a *concurrent* run
(lock busy) are never touched — the endpoint reports `PROCESSING` instead.

## Batching & performance (§10, §34)

- Reads: `iter_comments(video_id, batch_size, status="READY_FOR_ANALYSIS")`
  — the existing keyset reader; the dataset is never loaded whole
  (`SELECT *` over the full table never happens; memory is bounded by one
  batch).
- Batch size: `settings.comment_batch_size` (default **500**) — the existing
  configuration knob, no new hard-coded limit.
- Per batch: 1 claim write (one lock, one `executemany`, one commit) →
  N classifications (µs each) → 1 persist write (same shape). SQL-side
  `GROUP BY` aggregation afterwards.
- Measured: the run logs `SENTIMENT_RUN_COMPLETED` with real `elapsed_ms`
  and `comments_per_second` (null only for a true zero-duration run). A
  repository-spy test asserts writes arrive in chunks of ≤ `comment_batch_size`
  and every row exactly once.

## Idempotency (§12)

- `PROCESSED` rows are never re-read: a second GET with nothing pending
  returns byte-identical aggregates (tested).
- A content edit resets the row to `READY_FOR_ANALYSIS` **and clears all
  five sentiment columns** in the same upsert (a verdict describes the text
  that produced it), so re-analysis is forced exactly when the source
  changed. `like_count` refreshes do not reset anything.
- If a verdict write races a content reset, `save_sentiment_results`
  skips rows that are no longer `PROCESSING` — a stale verdict can never
  overwrite a fresh reset (tested).

## Aggregation (§14, §15)

Computed in SQL per video (`GROUP BY sentiment_label`, index
`idx_comments_video_sentiment`), then converted in three pure, unit-tested
helpers:

- **Percentages** use the **largest-remainder method executed in integer
  tenths of a percent**, so every value is a multiple of 0.1 and the three
  always sum to **exactly 100** when `analyzed > 0` (no floating-point
  artifacts in the API; verified in integer tenths by tests). The
  **denominator is `analyzed`** — never collected, never stored.
  Examples: `10/5/5 of 20 → 50.0/25.0/25.0`; `220/130/50 of 400 →
  55.0/32.5/12.5` (Sprint 4.1: whole values render as `64%`, fractional
  ones as `32.5%` — the extension never re-rounds).
- **Tie-break** for both the leftover tenth of a percent and
  `dominantSentiment` is the fixed label priority
  **POSITIVE > NEUTRAL > NEGATIVE** (first label reaching the shared
  maximum wins) — documented and deterministic: `1/1/1 → 33.4/33.3/33.3`.
- **`dominantSentiment` is `null` when nothing was analyzed** (empty
  dataset, not analyzed, all skipped/failed) — never a label without data.
- **Dataset metrics (Sprint 4.1)** ride along in a `dataset` block
  (`collected`/`stored`/`analyzed`/`skipped`/`failed`/`hasMore`/
  `limitReached`) so the UI can reconcile every displayed number against
  `stored == analyzed + skipped + failed + pending`. See
  docs/13-backend-api-contract.md for the exact semantics.

```json
{
  "analyzed": 2400,
  "positive": 1680, "neutral": 480, "negative": 240,
  "positivePercent": 70, "neutralPercent": 20, "negativePercent": 10,
  "dominantSentiment": "POSITIVE"
}
```

## Trigger design (§17) — backend-owned, least disruptive

**Sprint 4.3 (primary path):** `POST /api/videos/{id}/analysis` starts a
background job that acquires (if needed) and then runs the exact same
batched pipeline below on a worker thread; the extension polls
`GET /api/videos/{id}/analysis/status` for real progress and reads this
endpoint **after** `COMPLETED` (nothing pending → pure read). Processing
stays generation-guarded and cancellation-aware exactly as described
below — the job simply *is* the run now, off the request path.

**Compatibility path (kept):** `GET /api/videos/{id}/sentiment` is itself
the trigger:

1. validate id → 404 if no stored dataset;
2. if the video has pending rows (`READY_FOR_ANALYSIS` + `FAILED` +
   orphaned `PROCESSING`) **and** no run is active (non-blocking run-lock
   acquire), process them synchronously in batches, then read aggregates;
3. if the lock is busy (e.g. the background job owns the run), answer from
   current state with `PROCESSING`.

`GET /api/videos/{id}` is untouched (byte-compatible contract, verified by
its original tests), so Sprint 3's API stays intact while the new endpoint
sits alongside it (§16/§20). The extension flow is: detect video → fetch
video data → request sentiment → render (§20).

**Single active dataset (Sprint 4.2, §40/§41)**: the endpoint answers
strictly for its `video_id` and never activates a video. If the id is not
the active working dataset there are no stored rows → the existing
**404 `video_not_found`** (no blind activation, no stale aggregates). For
the active video, aggregates are SQL `GROUP BY` over that video's rows only
— cross-video leakage is structurally impossible after the switch deletes
the previous dataset.

## Lifecycle exposed by the API (§18)

| `status` | Meaning | UI copy |
|---|---|---|
| `NOT_ANALYZED` | no verdict yet (empty dataset or pending-but-not-run) | `READY TO ANALYZE` |
| `PROCESSING` | a run is active / rows claimed | `ANALYZING AUDIENCE` |
| `PROCESSED` | ≥ 1 verdict exists (possibly partial) | `ANALYSIS COMPLETE` |
| `FAILED` | nothing analyzed, ≥ 1 failure (next GET retries) | `ANALYSIS UNAVAILABLE` |

Derivation order (documented): active run → any `PROCESSING` rows →
`processed == 0` + failures → `FAILED` → `processed == 0` →
`NOT_ANALYZED` → else `PROCESSED`. A partial dataset (some rows still
pending after a content reset) reports `PROCESSED` with honest partial
stats — `totalComments` vs `analyzed` shows the gap.

## Cache compatibility (§19)

No new cache layer. Sentiment results are **derived from the persisted
dataset** and stored on it: once `PROCESSED` and unchanged, subsequent GETs
do zero model work (L1 memory is not even consulted for this endpoint).
A content change (repository upsert) is what re-arms processing — the
existing three-layer cache is untouched.

## Processing invalidation on video switch (Sprint 4.2, §38/§39)

A sentiment run belongs to the activation that started it. It captures the
video's `dataset_generation` before the first batch and re-verifies it
(`video_id AND is_active=1 AND dataset_generation = …`) under the database
lock before every batch flush and the final flush:

- **Superseded mid-run** → `SENTIMENT_RUN_SUPERSEDED` logged, the run
  aborts *before* committing the batch; verdicts of a no-longer-active
  video are never written anywhere (their rows were deleted by the switch,
  and a guard mismatch writes nothing).
- **A → B → A** (switch back): the old run still holds generation *n*, the
  new activation has *n+1* — the stale run cannot touch the fresh A rows;
  they honestly report `NOT_ANALYZED` until a new run processes them.
- The response is always built from the **current** generation's rows, so
  the API never reports counts that were superseded mid-request.

## Security (§28)

Parameterized SQL only (repository), validated video ids, no raw comment
text in logs or API responses, no secrets in the extension (it only GETs),
error envelopes stay friendly (`invalid_video_id` / `video_not_found` /
`storage_unavailable`), and representative raw comments are deliberately
**not** returned this sprint (§26: better to defer than to build an ad-hoc
exposure path).

## Testing

- **Engine** (`tests/test_sentiment.py`): positive/neutral/negative
  classification, empty text, determinism, score ∈ [-1,1], confidence =
  documented boundary distance, threshold self-consistency.
- **Aggregation**: 10/5/5 → 50/25/25, 1680/480/240 → 70/20/10, sums
  exactly 100, fractional-tie → POSITIVE, empty → `(0,0,0)` + dominant
  `null`, dominant ties by priority.
- **Repository** (`tests/test_repository.py::TestSentimentPersistence`):
  claim/persist fields, illegal targets never forced, content reset clears
  verdicts, like-count refresh keeps them, FAILED reprocessing, duplicate
  upserts don't duplicate verdicts, per-video isolation.
- **Service**: batch spy (25 rows in ≤ `comment_batch_size` chunks, each
  row once), language skip, idempotent rerun, per-row failure isolation,
  FAILED requeue, orphan recovery, log-safety (no raw text leaked).
- **API** (`tests/test_sentiment.py::TestSentimentEndpoint`): valid/empty/
  processing/processed/failed, 422/404, aggregation correctness, video
  isolation, original `/api/videos/{id}` contract intact.
- **Lifecycle (Sprint 4.2)** (`tests/test_active_lifecycle.py`): activation,
  same-video no-op, atomic switch + rollback, rapid A→B→C→D, generation
  guards, ingestion-session supersession, end-to-end API race, sentiment
  run supersession, migration — plus the rewritten isolation tests
  (`switch_replaces_the_working_dataset`, sentiment 404-after-switch).
- **Extension**: 126 tests (Sprint 1–4 tests unchanged and green + Sprint
  4.1 tests incl. the §41 denominator-accuracy case — *500 collected / 400
  analyzed must show 55% / 32.5% / 12.5%, never 44% / 26% / 10%* — the
  critical *Video A's 70% never appears under Video B* stale-navigation
  case, the §48 out-of-order case (B resolves first, late A discarded), the
  §53 `ACTIVE VIDEO` marker, and Sprint 4.3's job-flow suite: start →
  poll progress → complete, interrupted-job copy + retry, categorized start
  failure, polling abort + late-result discard on video switch, store
  stale/job-id guards, and the HTTP job client's bounded polling).

## Known limitations (honest)

- English-only verdicts (explicit policy above); other languages are
  counted as `skipped`, not interpreted.
- VADER is a lexicon heuristic: sarcasm, heavy slang, and code-switched
  text will be misread — no claim of human-level understanding.
- Inline processing on the GET path remains for compatibility, but the
  dataset-bound concern it carried is *resolved (Sprint 4.3)*: the primary
  trigger is the background job, which batches through `COMMENT_BATCH_SIZE`
  off the request path with visible progress — thousands of comments no
  longer hold any HTTP request open.
- Single-process assumption for orphan recovery (no cross-process lock).
- No representative comments, emotion, topics, or aspects — Sprint 5+.
