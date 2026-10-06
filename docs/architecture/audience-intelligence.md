# Audience Intelligence — Emotion · Intensity · Confidence · Mood (Sprint 5)

**Status:** implemented (`app/services/sentiment.py`, tests in
`backend/tests/test_audience_intelligence.py`) · **Date:** 2026-09-28 · **Version:** 0.5.0

Sprint 5 extends the existing sentiment stage — same pipeline, same batched
background job, same repository — with four additional intelligence axes.
Everything below is either **real model output** or a **documented
deterministic derivation**; nothing is random, hard-coded per video, or
LLM-generated.

```text
Comment (en, normalized) ──► VADER ──► sentiment label + compound + decision margin
                     └────► NRC lexicon ──► emotion label + share
                                             └─► derived: intensity band
                     └─► aggregate: distributions, dominant, confidence avg, mood
```

## Emotion model

- **Library:** `nrclex` ≥ 4.1 (NRC Emotion Lexicon, Mohammad & Turney,
  2013), installed via `requirements.txt`.
- **How it is driven:** `NRCLex().load_token_list(tokens)` — our own
  tokenizer (lowercase ASCII word extraction, `[a-z]+`, over the
  already-normalized text). This deliberately bypasses the library's
  TextBlob path, which requires NLTK corpora downloads (network on first
  run, machine-dependent data). The bundled JSON lexicon
  (`nrclex/data/nrc_en.json`, ~6.5k entries) is the only data used: pure
  Python, CPU-only, deterministic, no weights, no runtime network.
- **Supported labels (the real categories):**

  ```text
  FEAR · ANGER · ANTICIPATION · TRUST · SURPRISE · SADNESS · DISGUST · JOY
  + NEUTRAL (zero emotion-word hits in the comment)
  ```

  The lexicon also tags words `positive`/`negative`; those are **not
  emotions** — polarity is already the sentiment axis, so the emotion axis
  excludes them (a polarity-only word yields NEUTRAL, tested).
- **Per-comment result:** winning category = highest hit count over the 8
  categories; exact ties resolve to the first label in the lexicon's
  canonical order (fear → anger → anticipation → trust → surprise →
  sadness → disgust → joy), NEUTRAL last — deterministic, tested.
  `emotion_score` = winner's share of the comment's detected emotion
  evidence (`winner_hits / total_emotion_hits`, ∈ (0, 1]); 0.0 when there
  are no hits. It is a **proportion of lexicon hits, not a probability**.
- **Language:** same gate as sentiment — English rows only; unsupported
  rows carry no emotion at all (never "neutral emotion" by default).

## Sentiment intensity (derived)

Intensity represents **how strongly a comment expresses its sentiment**.
The model has no separate intensity signal, so it is a documented
deterministic band on the VADER compound score (§6 — derived, not
model-generated):

```text
|compound| < 0.35          → LOW      (VADER's neutral zone |c| < 0.05 is LOW:
                                        no/weak expression, never overstated)
0.35 ≤ |compound| < 0.70   → MEDIUM
0.70 ≤ |compound| ≤ 1.0    → HIGH
```

Boundaries are closed-low (0.35 → MEDIUM, 0.70 → HIGH), absolute value
(bands are symmetric), exact range `{LOW, MEDIUM, HIGH}`.

## Model confidence (existing genuine signal)

`sentiment_confidence` already exists per row (Sprint 4): VADER's
**decision margin** — normalized distance from the label boundary
(0 = sitting on the threshold, 1 = deep inside the band). It is a genuine,
deterministic model-derived signal, **not a calibrated probability**
(`sentiment-analysis.md`).

- API: `confidence.average` = mean of that margin over polarity-analyzed
  rows, rounded to 3 decimals; **null when nothing is analyzed** (never a
  fabricated 0).
- UI shows it as "MODEL CONFIDENCE nn%" with the caption
  "sentiment decision margin" so the metric is never oversold.

## Audience mood (deterministic rules, no LLM)

`audienceMood` ∈ `POSITIVE · CALM · EXCITED · MIXED · CONCERNED · NEGATIVE`,
computed from the real aggregates of the analyzed set. First matching rule
wins; all thresholds are module constants (`app/services/sentiment.py`):

```text
0. analyzed < 10                        → null  (no mood claimed, UI hides card)
1. balance = positive% − negative%
2. balance ≥ +25:
     HIGH intensity ≥ 30%                          → EXCITED
     or dominant emotion JOY/ANTICIPATION ≥ 25%    → EXCITED
     else LOW intensity ≥ 50%                      → CALM
     else                                          → POSITIVE
3. balance ≤ −25:
     HIGH intensity ≥ 30%                          → NEGATIVE
     or dominant emotion ANGER/DISGUST ≥ 25%       → NEGATIVE
     else                                          → CONCERNED
4. otherwise (−25 < balance < +25)                 → MIXED
```

Emotion share = `count / analyzed`. A NEUTRAL dominant emotion never
triggers the emotion branches (no emotion evidence). Worked examples and
every branch are unit-tested.

## Dominant emotion & summaries (§13/§14)

- **Dominant emotion:** highest count over emotion-analyzed rows; ties →
  lexicon canonical order (same rule as the per-comment winner); all-zero →
  null. `dominant_percent` is its share of the same set, from the shared
  largest-remainder distribution.
- **Intensity summary:** LOW/MEDIUM/HIGH distribution (counts + percents)
  plus `intensity.overall` = band with the largest share, ties → the
  weaker band (LOW > MEDIUM > HIGH: never overstate). All-zero → null.

## Aggregation denominators (§11)

All distributions use the **integer largest-remainder method** generalized
from Sprint 4 (multiples of 0.1, sums to exactly 100 for any non-empty
set, deterministic tie-break by the label's priority order):

| Metric | Denominator |
|---|---|
| sentiment % | `analyzed` (polarity verdicts) |
| emotion % / dominant % | emotion-analyzed rows (== `analyzed` by write-path invariant) |
| intensity % | intensity-tagged rows (== `analyzed`) |
| confidence average | polarity-analyzed rows |
| mood sample size | `analyzed` |

Write-path invariant: one outcome writes polarity + emotion + intensity
together, and UNSUPPORTED_LANGUAGE rows carry **all** of them NULL — so
`sum(emotion.counts) == sum(intensity.counts) == stats.analyzed` whenever
the dataset is fully processed (asserted in tests). Skipped/failed/pending
rows never enter any denominator; unsupported language is never counted as
neutral emotion.

## Database changes (§9)

Additive columns on `comments` (no new tables/databases/repositories):

```text
sentiment_intensity  TEXT     LOW | MEDIUM | HIGH   (derived)
emotion_label        TEXT     NRC category | NEUTRAL
emotion_score        REAL     winner share ∈ [0, 1]
emotion_model        TEXT     'nrclex-4.1' (provenance)
```

Applied through the existing PRAGMA-guarded `_MIGRATIONS` (idempotent).
Content resets clear the four columns together with the sentiment columns
(they are all derived from the same text).

**One-time legacy reopen:** when `emotion_label` is added for the first
time, rows already `PROCESSED` predate emotion/intensity; they are moved
`PROCESSED → READY_FOR_ANALYSIS` with the sentiment columns cleared (the
same reopen the content-reset performs). The next run — sync GET or
background job — regenerates everything deterministically. Raw text is
untouched; the guard makes the reopen fire exactly once (never on later
startups, never on fresh databases). This preserves the invariant that
`PROCESSED` rows always carry the full intelligence set.

## API changes (§15)

`GET /api/videos/{id}/sentiment` keeps its entire Sprint 4 contract and
adds four **defaulted** blocks (old clients ignore them; a pre-Sprint-5
client validates unchanged):

```jsonc
{
  "videoId": "…", "status": "PROCESSED",
  "stats": { /* unchanged */ },
  "dataset": { /* unchanged */ },
  "dominantSentiment": "POSITIVE",
  "emotion": {
    "dominant": "JOY",
    "dominantPercent": 40.0,
    "distribution": { "FEAR": { "count": 1, "percent": 10.0 }, /* …9 keys */ }
  },
  "intensity": {
    "overall": "MEDIUM",
    "distribution": { "LOW": { "count": 3, "percent": 30.0 }, /* … */ }
  },
  "confidence": { "average": 0.842 },
  "audienceMood": "EXCITED"
}
```

`audienceMood` is null below the 10-comment sample; `confidence.average`
is null when nothing is analyzed; `emotion.dominant` /
`intensity.overall` are null when their axis has no rows.

## Job-processing behavior (§10/§24)

Emotion + intensity run **inside the same per-row pass** as sentiment
(`classify → classify_emotion → intensity_for`) in the existing batched,
generation-guarded run — one worker thread, batches of
`comment_batch_size`, persisted per batch, cancellation/active-video checks
before every batch (Sprint 4.2/4.3 machinery unchanged). There is no new
phase in the job protocol: sentiment and emotion are one inference pass,
so splitting them into separate progress stages would fabricate timing.
The UI's existing truthful states (ACQUIRING → analyzing with real counts)
are unchanged; aggregates are computed at read time by SQL.

## Limitations (documented, §27)

- **Bag-of-words:** no negation, sarcasm, or context — "not good" still
  hits `good`; such comments keep their (honest) lexicon output.
- **Exact surface matching:** no lemmatization — inflected forms absent
  from the lexicon (`loved`, `great`) are missed; a zero-hit comment is
  NEUTRAL emotion, never a guessed one. Measured hit rate on realistic
  English comment fixtures: ~87% (14/16).
- **English only** (same language policy as sentiment); unsupported
  languages are skipped, not translated.
- Confidence is a decision margin, not a calibrated probability; mood is
  a rule over three aggregate signals — it is intentionally coarse and
  claims nothing below 10 analyzed comments.
- Emotion vocabulary is fixed by the NRC lexicon (no EXCITEMENT/
  CONFUSION/FRUSTRATION-style labels — those would be invented).
