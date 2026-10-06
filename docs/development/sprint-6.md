# Sprint 6 — Topic, Discussion & Audience Preference Intelligence

**Status:** complete · **Date:** 2026-10-01 · **Version:** 0.6.0

## What This Sprint Delivers

A genuine, lexically-driven NLP topic-discovery layer over the comments the
pipeline already analyzed: **what the audience talks about, what they love,
what they repeatedly criticize, and which discussions are contested** — with
per-topic sentiment/emotion/intensity roll-ups. Pure Python (no new
dependencies, **no new tables**, no LLM), computed on read over the existing
`comments` table and memoized by a `(count, max(processed_at))` fingerprint.
At 5,000 comments a cold GET builds the whole analysis in **692 ms**
(4.0 MB peak); a warm memo hit is **~0.3–2.9 ms**.

## 1. Inspection (no guessing)

| Question | Answer (verified) |
|---|---|
| Data source | Existing `comments` rows for the video — text, normalized text, language gate, per-comment `sentiment`, `emotion`, `intensity`, `confidence` already written by Sprints 4–5 |
| Where it fits | New read-only service (`TopicService`) next to `SentimentService`; no pipeline, job-write, or schema changes |
| Job system | `JobPhase` enum extended with `TOPIC` (backend + `src/shared/types.ts`); warm-up runs between sentiment and `COMPLETE` |
| Frontend | React overlay with the Sprint 5.2 progressive-insight view; topics section renders after sentiment when `status === PROCESSED && analyzed > 0` |
| Constraints honored | §14 quality rules (no fabricated text), §39 (topic failure logs `TOPIC_WARM_FAILED`, job still completes), zero new dependencies |

## 2. Approach & Why

Embeddings/transformers were rejected (CPU cost, new deps, §14). Instead:

1. **Candidate phrases** — bigrams/trigrams from normalized comment text
   (URLs/@mentions stripped, `[a-z]+` tokens, length 2–14, 4-digit numbers
   kept as tokens), pruned by document frequency: df ≥ `_MIN_CANDIDATE_DF=3`,
   top `_MAX_CANDIDATES=400` by df×idf-ish score against generic/stopword
   sets. Single-token candidates only survive if df ≥ 6 and not generic.
2. **Clustering** — greedy token-Jaccard agglomeration at
   `_MERGE_JACCARD=0.6`, but only between clusters passing a **symmetric
   size-ratio pre-filter** and a **co-comment overlap gate**
   (`_MERGE_DOC_OVERLAP=0.7` of the *larger* cluster, ≥
   `_MERGE_MIN_SHARED_DOCS=3` shared comments), plus a final
   **containment duplicate-absorption pass**. This was the hard-won lesson:
   naive overlapping-similarity merges glued hub words ("failed", "install")
   across unrelated themes; the hygienic design keeps themes separate and
   tests use vocabulary with no shared tokens between themes.
3. **Label** — highest-df representative phrase per cluster; support =
   distinct comments mentioning any cluster phrase.

Purely lexical ⇒ grouping is **not distributional** (see limitations), but
it is deterministic, explainable, and runs in milliseconds.

## 3. Categories (mutually exclusive gates, §11/§14/§§15)

| Category | Gate | Ranking |
|---|---|---|
| `APPRECIATED` | positive share ≥ 60% | PreferenceScore `0.6·freq_norm + 0.4·pos_ratio`, gated by positive mentions ≥ `topic_min_support` (top 4) |
| `PAIN_POINT` | negative share ≥ 40% | symmetric score, negative mentions ≥ min_support (top 4) |
| `MIXED` | both sides ≥ 20% with pos < 60 & neg < 40 | by mentions (top 4) |
| `MOST_DISCUSSED` | else | by mention count (top 6) |

Confidence per topic = `0.5·support_norm(reach at 3×min_support) +
0.5·cluster cohesion` — documented explicitly as **not** a sentiment
confidence. Raw scores are never exposed; only counts, shares, confidence,
dominant emotion + intensity roll-ups (share-weighted), and example-evidence
counts.

## 4. Configuration (`app/core/config.py`)

- `topic_min_comment_count=30` (validated ≥ 10) — below this the section
  shows the honest "Not enough repeated discussion yet." notice.
- `topic_min_support=8` (validated ≥ 2) — mention floor for
  appreciated/pain ranking.
- `topic_max_topics=24` (validated 2..64) — hard cap after ranking.

## 5. API & Job Changes

- **`GET /api/videos/{video_id}/topics`** → `TopicAnalysisResponse`
  (status, analyzed count, four ranked sections, topic items with
  share/sentiment/emotion/intensity/confidence/evidence).
- `JobPhase.TOPIC` added; job worker runs a topic warm-up between sentiment
  and `COMPLETE` so the first UI render hits the memo. Failure →
  `TOPIC_WARM_FAILED` log, job still `COMPLETED` (§39).
- `get_analysis_fingerprint` returns `(count, max(processed_at))` — the
  memo key; any new/edited comment invalidates it.

## 6. Frontend UX

- New `TopicsService` port + `HttpTopicsService` (+ structural validation)
  and `UnconfiguredTopicsService`; wired positionally through
  `mountApp(store, analysis, sentiment, job, topics, gate)`.
- `topics` slice on the overlay store with `begin/complete/fail/clearTopics`
  (video-id guards; A→B video switches can never paint stale topics —
  covered by dedicated tests) and reset in both `setVideoContext` branches.
- [src/ui/components/topics.tsx](../../src/ui/components/topics.tsx): four
  titled groups (What people talk about / What people loved / Pain points /
  Mixed discussions), rank chips 01…, mention bars, stacked mixed bars,
  expandable rows → share, ideas chips, per-topic sentiment bars, dominant
  emotion + intensity with confidence and evidence; `TopicLoading`
  ("Discovering audience discussions…"), `TopicError` (+Retry),
  `TopicNotice` (empty). Phase line in the progressive view says
  "discovering discussions…" during `TOPIC`.
- `onTopicsRetry` = `clearTopics` (retriggers the load effect);
  `onSentimentRetry` also clears topics so a sentiment refresh can't pair
  with stale topic data. Footer shows "Sprint 6".
- ~500 lines of CSS in `overlay.css` (`.sai-card-topics`, group/kind
  colors, mixbar, detail, state, ≤1366px media query; reduced-motion
  covered by the existing global rule).

## 7. Measured Performance (`backend/benchmarks/bench_topics.py`,
results in `benchmarks/results/sprint6-topics.json`, min of 5 runs)

| comments | extract | cluster | aggregate | db load | **service total** | memo hit | peak mem |
|---|---|---|---|---|---|---|---|
| 100 | 2.9 ms | 2.0 ms | 0.6 ms | 7.0 ms | **10.2 ms** | 0.19 ms | 0.2 MB |
| 500 | 14.6 ms | 5.7 ms | 0.6 ms | 29.0 ms | **52.7 ms** | 0.36 ms | 0.6 MB |
| 1,000 | 31.9 ms | 7.3 ms | 0.6 ms | 62.8 ms | **108.5 ms** | 0.55 ms | 0.9 MB |
| 2,500 | 87.3 ms | 39.3 ms | 1.2 ms | 159.2 ms | **249.8 ms** | 2.9 ms | 2.4 MB |
| 5,000 | 183.9 ms | 6.6 ms | 0.9 ms | 405.0 ms | **691.8 ms** | 2.6 ms | 4.0 MB |

Embedding time = 0 by design (no embeddings). Aggregation (categories +
ranking + roll-ups) is ≤ 1.2 ms at every scale. The dominant cold cost is
the SQLite row load; everything after it is sub-200 ms even at 5,000.

## 8. Tests Executed (real results)

| Suite | Command | Result |
|---|---|---|
| Backend | `cd backend && ./.venv/Scripts/python -m pytest -p no:warnings -q` | **422 passed, 4 skipped** (+44 new in `tests/test_topics.py`) |
| Extension | `npm run verify` | typecheck clean, **180 passed / 13 files** (+14 in `src/ui/topics-ui.test.tsx`), build ✓ (337.14 kB) |

New coverage: extraction hygiene (URLs/@mentions/numbers/stopwords),
frequency + df gates, all four category gates and mutual exclusivity,
appreciated/pain ranking, aggregates/roll-ups, service build + fingerprint
memo (and invalidation), active-video guards, HTTP API contract, job phase
incl. failure-tolerant warm-up, perf ceilings at 100–5,000 comments, UI
rendering for all four groups/expanded card/loading/error/empty, A→B stale
protection, minimize/restore, phase copy, unconfigured (no-transport)
absence.

## 9. Visual QA (preview harness, `vite.preview.config.ts`, port 5199)

Verified in the browser, not just tests:

- **Default scene** — full section: four groups, rank chips 01/02/03,
  mention bars with shares computed against **analyzed** (25.8% etc. —
  fixed a fixture bug that divided by mentions), mixed stacked bar.
- **Expanded card** — share, ideas chips, per-topic sentiment bars,
  dominant emotion TRUST 41.2%, intensity LOW 39.3%, confidence, evidence;
  sentiment section intact above.
- **`?scene=topics-error`** — "Audience topics unavailable" + Retry with
  sentiment data intact.
- **`?scene=topics-empty`** — "Not enough repeated discussion yet."
- **`?scene=topics-loading`** — "Discovering audience discussions…"
  ("Grouping repeated themes from the analyzed comments.") while sentiment
  renders above and coverage below.
- **Responsive** — verified at 1300 px wide (≤1366 media query): layout
  holds, footer reads "Sprint 6".

## 10. Known Limitations

- **Lexical, not distributional** — synonyms that share no token
  ("price"/"cost") stay in separate clusters; grouping quality depends on
  the audience's own vocabulary. English-only for the same reason (token
  regex + English stopword sets), consistent with the pipeline's language
  gate.
- **Themes are not a partition** — a comment can mention several topics;
  section shares can sum > 100% by design (share = comments mentioning the
  topic / analyzed).
- **Compute-on-read** — no table means every cold GET after a fingerprint
  change pays the full build (692 ms at 5,000: ~405 ms db load + ~190 ms
  extraction); warm hits are ~0.3–2.9 ms. A persisted `topics` table would
  remove the cold cost at the price of write-path complexity — deliberately
  not taken (§"no new tables" scope).
- Confidence is support×cohesion, **not** a sentiment probability; raw
  ranking scores are intentionally never returned.
- Topic failure never fails the job (§39) — a permanently failing topic
  build would leave the section in its Retry state until manually retried.
