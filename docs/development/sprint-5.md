# Sprint 5 — Advanced Sentiment Intelligence Engine

**Status:** complete · **Date:** 2026-09-28 · **Version:** 0.5.0

## What This Sprint Delivers

```text
Comment → Sentiment → Emotion → Intensity → Confidence → Aggregated Audience Intelligence
```

The existing POSITIVE/NEUTRAL/NEGATIVE verdict gains three intelligence
axes — **emotion** (real NRC lexicon inference), **intensity** (documented
derivation from the VADER compound), and surfaced **confidence** (the
existing genuine decision margin) — plus a deterministic **audience mood**
over the real aggregates. Same pipeline, same batched background job, same
repository, same overlay shell: extended, not rebuilt. Zero-click (Sprint
4.4) untouched — still **no Analyze button**.

## Files / Modules Changed

**Backend**
- `app/services/sentiment.py` — `classify_emotion()` (NRC lexicon via
  `nrclex` `load_token_list`, own tokenizer), `intensity_for()` bands,
  `audience_mood()` rules, generalized largest-remainder
  `_percentages()`/`label_distribution()`/`dominant_label()`, extended
  `_build_response()` (emotion/intensity/confidence/mood blocks) and
  `_process_batch()` (same per-row pass now writes emotion + intensity).
- `app/db/schema.py` — 4 additive columns on `comments`
  (`sentiment_intensity`, `emotion_label`, `emotion_score`,
  `emotion_model`) + **one-time** legacy reopen (PROCESSED → READY with
  verdicts cleared, guard = "emotion_label was just added").
- `app/db/repository.py` — `SentimentOutcome` gained defaulted Sprint 5
  fields; save/reset SQL covers the new columns; new SQL-side aggregates
  `get_emotion_counts`, `get_intensity_counts`,
  `get_average_sentiment_confidence`.
- `app/models/internal.py` — `EmotionBreakdown`, `IntensityBreakdown`,
  `ConfidenceBreakdown`, `LabelShare`, vocabulary literals; 4 defaulted
  fields on `SentimentAnalysisResponse`.
- `requirements.txt` — `nrclex>=4.1` with rationale.

**Extension**
- `src/shared/types.ts` — Sprint 5 types; `SentimentAnalysis` gained
  *optional* blocks (pre-Sprint-5 bodies keep validating).
- `src/services/analysis/http-sentiment-service.ts` — structural
  validation of the optional blocks (present-but-garbage →
  `upstream_data_invalid`; absent → valid).
- `src/ui/components/OverlayBody.tsx` — four cards in the results view
  (Dominant Emotion, Audience Mood, Emotional Intensity with counts,
  Model Confidence with honest caption); each renders only from its
  backend block.

**Docs** — `docs/architecture/audience-intelligence.md` (new, the §27
reference), `docs/13-backend-api-contract.md`, `README.md`,
`docs/development/sprint-5.md`.

## Database Changes

Additive-only on `comments` (SQLite, PRAGMA-guarded idempotent
migrations — no new tables/DBs/repositories/indexes; aggregates ride the
existing per-video indexes):

| Column | Type | Meaning |
|---|---|---|
| `sentiment_intensity` | TEXT | `LOW · MEDIUM · HIGH` (derived) |
| `emotion_label` | TEXT | NRC category or `NEUTRAL` (zero hits) |
| `emotion_score` | REAL | winner's share of detected emotion evidence |
| `emotion_model` | TEXT | `nrclex-4.1` provenance |

Existing data stays safe: content resets clear the four columns together
with sentiment, and pre-Sprint-5 `PROCESSED` rows reopen **exactly once**
for deterministic reprocessing (raw text untouched).

## API Changes

`GET /api/videos/{video_id}/sentiment` — entire Sprint 4 contract
unchanged; four defaulted additions: `emotion` (dominant +
dominantPercent + 9-label count/percent distribution), `intensity`
(overall + 3-band distribution), `confidence.average` (null when
unavailable), `audienceMood` (null below the 10-comment sample).

## AI Model / Library

**NRC Emotion Lexicon (Mohammad & Turney, 2013) via `nrclex` ≥ 4.1.**
Driven through `load_token_list()` with the pipeline's own lowercase-word
tokenizer — bundled JSON lexicon only, no NLTK/TextBlob corpora, no
runtime downloads, fully deterministic. Real labels:
`FEAR · ANGER · ANTICIPATION · TRUST · SURPRISE · SADNESS · DISGUST · JOY`
+ `NEUTRAL` (zero-hit). Polarity tags are excluded (they belong to the
sentiment axis). Derived metrics (intensity bands, mood rules, confidence
semantics) are documented in `audience-intelligence.md` §rules.

## UI Changes

Results view only, existing visual language (hud headings, `sai-inset`
cards, `sai-bars` rows, existing palette): **Dominant Emotion** (label +
share of analyzed + model caption), **Audience Mood** (value + "derived
from…" caption), **Emotional Intensity** (Low/Medium/High bars with real
counts + overall badge + denominator line), **Model Confidence**
(percentage + "sentiment decision margin" caption). Cards disappear
individually when their block/null is absent. Loading states unchanged —
the truthful Sprint 4.3 phases already cover the single sentiment+emotion
pass (no fabricated sub-stages). FAB untouched.

## Tests Executed (§26 — real results)

| Suite | Command | Result |
|---|---|---|
| Backend | `./.venv/Scripts/python -m pytest -p no:warnings` | **313 passed, 4 skipped** (256 pre-Sprint-5 + 57 new) |
| Real API (opt-in) | `RUN_REAL_API_TESTS=1 pytest tests/test_real_api.py` | **4 passed** |
| Extension | `npm run verify` | **155 passed, 11 files** + typecheck + build (142 pre-Sprint-5 + 13 new) |

New coverage (§25): emotion real inference (JOY/DISGUST/NEUTRAL,
polarity-excluded, tokenizer, tie-breaks, determinism), intensity exact
boundaries (0.34/0.35/0.69/0.70, negatives, neutral zone), confidence
average + unavailable, distribution denominators/rounding/empty/partial,
every audience-mood branch + insufficient sample, batched run persistence
with emotion, per-row inference failure isolation, model-level failure →
FAILED without crashing, content-reset clearing, active-video aggregate
isolation, migration one-time reopen idempotency, API blocks +
camelCase + old contract, background-job path publishing intelligence,
UI cards from real blocks / hidden when absent / per-card null handling,
service validation of garbage blocks, and the zero-click/no-Analyze
regression (all suites).

## Known Limitations

- Bag-of-words emotion: no negation/sarcasm/context ("not good" still
  hits `good`).
- Exact surface matching (no lemmatization): lexicon-missing inflections
  (`loved`, `great`) are missed → zero-hit comments honestly report
  NEUTRAL emotion; measured ~87% hit rate on realistic English fixtures.
- English-only (existing language policy); no translation.
- Confidence is a decision margin, not a calibrated probability (stated
  in UI caption + docs).
- Mood is intentionally coarse (3 aggregate signals, min sample 10).
- Legacy databases reprocess once on first startup after upgrade.

## Recommended Follow-ups (NOT in this sprint)

Topic extraction/aspect intelligence, emotion-language expansion,
confidence calibration study, mood history/trends — all later sprints per
§28 scope freeze.
