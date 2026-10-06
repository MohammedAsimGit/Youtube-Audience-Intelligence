# Sprint 5.2 — NLP Inference Speed Optimization

**Status:** complete · **Date:** 2026-09-30 · **Version:** 0.5.0

## What This Sprint Delivers

A benchmark-first optimization of the analysis stage only — same models
(VADER + NRC lexicon), same outputs (proven byte-for-byte), same SQLite
writes, same progressive UI, same zero-click flow. Measured on paired
same-session runs: **analysis throughput +38.7% (full drain) and +43.8%
on the per-page pattern, with per-batch latency −27%**; vs the full
pre-optimization baseline the analysis phase is **4.6× faster at 5,000
comments** (4.10 s → 0.88 s). No model replacement, no new dependencies,
no second pipeline.

## 1. Inspection (no guessing)

| Question | Answer (verified) |
|---|---|
| Model | VADER (`vaderSentiment`, lexicon+rules) + NRC Emotion Lexicon (`nrclex` 4.1) — pure Python, **CPU-only, no torch/transformers, no gradients to disable (§6 N/A), no GPU path (§12: nothing to detect)** |
| Loaded where/how | Lazy process-wide singletons (`_get_analyzer`, `_get_emotion_engine`) — **once per process**, never per comment/batch (§3 already satisfied; load = ~134 ms each, paid once) |
| Tokenizer | VADER: C-level `str.split` + punctuation strip inside `SentiText` per comment; emotion: compiled `[a-z]+` regex per comment. Lexicon models have **no batched tokenizer API** (§7 N/A — no padding/truncation concepts apply) |
| Inference shape | One comment at a time inside a **batched claim→classify→persist cycle** (§4: batching exists at the pipeline level; the libraries expose no tensor-batch call) |
| Inference batch size | `SENTIMENT_INFERENCE_BATCH_SIZE` (0 = follow `COMMENT_BATCH_SIZE`=500), benchmarked 32..512 |
| UI update | Job poll + throttled aggregate refresh (1.5 s, only on growth) — small; **not** the bottleneck |

## 2. Root Cause (cProfile, 2,000-comment drain, before changes)

```
analysis 1.21 s / 2000 rows
├─ VADER polarity_scores      0.90 s  (74%)  ← THE bottleneck
│    663k str.lower() calls — _negation_check/_special_idioms_check/
│    _but_check each REBUILD the whole lowered word list per call,
│    plus a per-character emoji scan even with no emoji
├─ emotion (NRCLex)           0.15 s  (12%)  — load_token_list builds
│    affect_list/dict/percent/top_emotions that this pipeline never reads
├─ SQLite (claim+save)        0.11 s  (9%)   — already batched, no N+1
└─ bookkeeping                ~5%
```

Secondary: interim analysis ran **one small drain per ~100-comment
YouTube page** (per-call fixed overhead ×50 for a 5,000-comment video),
and the UI's `analyzed` counter advanced every ~100 because of it.

## 3. Changes Made

1. **`app/services/vader_fast.py` (new)** — `FastSentimentIntensityAnalyzer`:
   exact-parity subclass that computes the lowered word list **once per
   comment** and threads it through line-for-line copies of
   `sentiment_valence` / `_negation_check` / `_special_idioms_check` /
   `_least_check` / `_but_check`; emoji conversion skipped via a
   precomputed frozenset when the text contains no emoji character.
   Lexicon, tables, damping, rounding — all inherited, unchanged.
2. **`app/services/sentiment.py`** — `_get_analyzer()` now builds the fast
   scorer; `classify_emotion` counts NRC label hits **directly from the
   singleton engine's bundled lexicon** (feature-detected `__lexicon__`,
   falls back to stock `load_token_list` automatically), skipping nrclex's
   unused per-call bookkeeping; `_EMOTION_DIRECT` A/B switch.
3. **`app/core/config.py`** — `ANALYSIS_PROGRESS_UPDATE_INTERVAL`
   (default **500**, validated 1..10000): minimum pending rows between
   interim drains during acquisition.
4. **`app/services/jobs.py`** — interim cadence gate: the **first drain of
   a job is immediate** (time-to-first-insight preserved), then drains
   re-arm every `ANALYSIS_PROGRESS_UPDATE_INTERVAL` pending rows;
   bootstrap resets per job. The final ANALYZING drain is unconditional —
   nothing is ever skipped.
5. **`benchmarks/bench_pipeline.py`** — `--baseline` now also disables the
   Sprint 5.2 switches (stock VADER + stock nrclex path) for A/B runs.

**Not changed (verified already correct):** model load-once, batched claim
and save (one transaction per batch, no N+1), PROCESSED reuse /
incremental analysis, active-video generation guards, zero-click, all API
contracts, all frontend code.

## 4–6. Measured Results (paired, same session)

**Per-comment micro A/B** (`micro` mode, identical command, n=2000):

| | before | after | Δ |
|---|---|---|---|
| VADER classify | 522.3 µs | **247.4 µs** | **−53%** |
| Emotion classify | 91.7 µs | **43.8 µs** | **−52%** |

**Parity:** 0 mismatches over 5,027 texts (5,000-comment benchmark corpus
+ adversarial negation/idiom/caps/emoji set) for VADER; 0 over 2,960
texts for emotion raw counts — every score byte-identical (enforced by
`tests/test_vader_fast.py` and `TestEmotionDirectParity`).

**5,000-comment patterns** (mean of 2 alternating rounds,
`post52-paired-ab.json`):

| Pattern | before c/s | after c/s | Δ | batch p50 |
|---|---|---|---|---|
| Full drain | 2,823 | **3,916** | **+38.7%** | — |
| Per-page drains (old cycle) | 2,265 | **3,257** | **+43.8%** | 35.6 → **26.1 ms** |
| Gated at 500 (new cycle) | 3,143 | **3,852** | +22.6% | 114 ms (10 drains, not 50) |

**Stages benchmark, 100..5000** (`--baseline` disables all 5.1+5.2
optimizations; analysis phase seconds):

| size | analysis before | analysis after | analyzed/s before → after |
|---|---|---|---|
| 100 | 0.070 | 0.060 | 1,428 → 1,662 |
| 500 | 0.261 | 0.097 | 1,914 → 5,174 |
| 1,000 | 0.722 | **0.307** | 1,385 → 3,255 |
| 2,500 | 1.469 | **0.947** | 1,702 → 2,640 |
| 5,000 | 4.099 | **0.884** | 1,220 → **5,654** |

**End-to-end with 250 ms simulated API latency/page** (`post52-e2e`):
first insight **0.74–1.5 s at every size including 5,000** (bootstrap
drain intact through the cadence gate — no regression vs Sprint 5.1's
1.4–1.6 s); all jobs COMPLETED, analyzed+skipped == total, 0 failed.
Totals: 1,000 → 7.6 s, 5,000 → 23.6 s (latency simulated in-harness).

**Batch-size sweep (32..512, after):** 3,662 / 3,995 / 3,150 / 4,949 /
4,048 c/s — no single optimum beyond the existing plateau band; default
(= `COMMENT_BATCH_SIZE` 500) kept, tuning via `SENTIMENT_INFERENCE_BATCH_SIZE`.

**Memory (tracemalloc):** bounded and unchanged — 1,000 → 2.3/1.9 MiB,
2,500 → 4.5/2.4 MiB (acquire/analyze peaks; 500-row figures are the usual
first-run import artifact).

## 7. UI Update Interval (§9/§10)

Inference batching (`SENTIMENT_INFERENCE_BATCH_SIZE`, default 500) and
progress cadence are now separate: after the immediate first drain,
`analyzed` advances in **~500-comment truthful steps**
(`ANALYSIS_PROGRESS_UPDATE_INTERVAL=500`), instead of one update per
~100-comment page — the frontend's existing 1.5 s-throttled,
only-on-growth refresh rides those steps unchanged (§21: aggregated
progress, no fake %). `=1` restores the old per-page cadence.

## Tests Executed (real results)

| Suite | Command | Result |
|---|---|---|
| Backend | `./.venv/Scripts/python -m pytest -p no:warnings` | **378 passed, 4 skipped** (328 + 50 new) |
| Real API (opt-in) | `RUN_REAL_API_TESTS=1 pytest tests/test_real_api.py` | **4 passed** (79 s) |
| Extension | `npm run verify` | **156 passed, 11 files** + typecheck + build ✓ |

New coverage: VADER exact parity (39 adversarial inputs + 500-text
corpus + failure-mode parity + singleton reuse), emotion direct-vs-stock
count parity + verdict parity + missing-attribute fallback, interval
config validation (4), interim cadence over HTTP (bootstrap-only gate
held to 1 page; gate re-arm at interval mid-fetch; final drain completes
all rows).

## Known Limitations / Remaining Bottlenecks

- **Language detection still dominates end-to-end time** (~20 s of the
  22 s total at 5,000 in the stages run) — unchanged by design here
  (§15 of Sprint 5.1 scope); it is the next real lever.
- VADER/emotion are inherently per-comment Python loops; further gains
  require a different model class (rejected under §14 quality rules —
  transformers are slower on CPU and would change outputs).
- Benchmarks show ±15–30% run-to-run CPU-state noise on this machine;
  only paired same-session numbers are claimed.
- Direct emotion counting feature-detects nrclex's `__lexicon__`; a
  future library change falls back automatically and fails the parity
  test loudly.
