# Sprint 7 — AI Audience Summary & Evidence-Based Insight Engine

**Status:** complete · **Date:** 2026-10-02 · **Version:** 0.7.0

## What This Sprint Delivers

A provider-independent **insight engine** that phrases the structured signals
Sprints 4–6 already computed into a short, human-readable audience briefing:
headline, summary, and six evidence-linked cards (overall reaction, what
worked, main discussion, pain point, emotional signal, takeaway), each backed
by evidence lines the user can open ("Why?"). The engine **never re-analyzes
comments and never sees raw comment text** — it receives an
`AudienceEvidence` snapshot of aggregates, produces a draft behind an
`InsightProvider` protocol, validates that draft against the evidence
(schema + numeric grounding + banned-claim language), and only then serves
it. The default `DeterministicProvider` is template-based and offline; an
optional `OpenAICompatibleProvider` (OpenAI-compatible chat completions) is
selected purely by configuration. Every response reports an honest `source`
(`deterministic` | `llm` | `fallback`) so the UI can never present
deterministic text as AI-generated. At 5,000 comments a cold GET builds the
whole analysis in **253 ms**; a warm memo hit is **~0.1–1.6 ms**.

## 1. Inspection (no guessing)

| Question | Answer (verified) |
|---|---|
| LLM already present? | **No** — repo only had `YOUTUBE_API_KEY`; no provider, client, or prompt code existed |
| Data source | Existing `SentimentAnalysisResponse` + `TopicAnalysisResponse` (Sprints 4–6) — aggregates only, no comment rows |
| Where it fits | New read-only `InsightService` next to `SentimentService`/`TopicService`; no schema changes, no pipeline changes |
| Job system | `JobPhase.INSIGHT` added (backend enum + `AnalysisJobPhase` in `src/shared/types.ts` + `JOB_PHASES`); warm-up runs after `TOPIC`, failure logs `INSIGHT_WARM_FAILED` and the job still completes (§39) |
| Frontend | New `InsightService` port + `HttpInsightService`; `insight` slice on the overlay store; section renders after sentiment, before topics |
| Constraints honored | §14 (no fabricated claims), §23/§45 (source honesty), §35 (technical-info rows), zero new dependencies for the deterministic path |

## 2. Approach & Why — the layered pipeline

```
SentimentAnalysisResponse + TopicAnalysisResponse   (source of truth)
    → AudienceEvidence        structured snapshot (Section 5)
    → InsightCandidates       deterministic rules/ranking (Section 6)
    → InsightProvider         phrasing behind a Protocol (Section 12)
    → validated InsightDraft  schema + grounding (Sections 7/15)
    → cards + evidence lines  built deterministically (Section 15/20)
```

- **No raw comments.** The phrasing layer sees only aggregates — bounded
  prompt, no per-comment text, no token blowup; cost is independent of
  comment count (prompt measures 407–413 bytes from 100 → 5,000 comments).
- **Provider-independent.** `InsightProvider` is a tiny protocol
  (`generate(evidence, candidates) -> InsightDraft`). The default
  `DeterministicProvider` fills templates over the evidence — no network, no
  key, always available. `OpenAICompatibleProvider` (httpx) is an optional
  adapter chosen by `INSIGHT_PROVIDER`, never hardcoded elsewhere.
- **Grounding before serving.** `validate_draft` rejects any draft that
  fails schema (`InsightDraft`, `extra=forbid`), breaches budgets
  (headline 2–12 words, bodies ≤ 500 chars, summary ≤ 700), contains banned
  claim language (regex: *everyone / best / caused by / recommends …*), or
  uses numbers **not present in `evidence.allowed_numbers()`** (canonical
  `.1f`/`.2f`/`%g`/int renderings). A rejected LLM draft retries within the
  bounded attempt budget, then falls back to deterministic text.
- **Honest source (§23/§45).** Every response carries
  `source = deterministic | llm | fallback`:
  - `deterministic` — default offline generator;
  - `llm` — validated model output (UI badge says "AI-generated");
  - `fallback` — model configured but failed/timed out/invalidated;
    deterministic text was served instead (badge must not say AI-generated).
  `ProviderError(permanent=True)` (4xx, missing key, banned claims) stops
  retries after a single call; transient errors (timeout, 5xx) use the
  bounded retry budget (default 2 attempts).
- **Active-video safety (§26/§27).** The service memo is keyed
  `(video_id, analyzed, latest)` with a fast-path fingerprint check before
  the sentiment read and a re-read after (a GET may run the engine inline);
  store slices guard every transition by `videoId`, so A→B switches can
  never paint stale insight (dedicated tests cover the interleavings).

## 3. Evidence model

`AudienceEvidence` (frozen snapshot) contains: sample sizes
(collected/analyzed/skipped), sentiment distribution + dominant label +
margin, dominant emotion + share, intensity distribution, and up to
`topic_max_topics` `TopicRef`s (id, label, mentions, share, category,
sentiment split, dominant emotion). `allowed_numbers()` pre-computes every
number the phrasing layer is allowed to mention, so grounding is a set
lookup rather than fuzzy matching. Candidate selection
(`select_candidates`) ranks one overall reaction, one appreciated theme, one
main discussion, one pain point, one emotional signal, and one takeaway —
reusing the Sprint 6 thresholds (`topic_min_comment_count=30`,
`topic_min_support=8`); below the sample floor the endpoint returns
`status=INSUFFICIENT_DATA` with an honest message instead of a thin
briefing.

## 4. Configuration (`app/core/config.py`)

| Setting | Default | Bounds | Purpose |
|---|---|---|---|
| `insight_provider` | `deterministic` | `deterministic` \| `openai_compatible` | provider selection |
| `insight_api_base_url` | `https://api.openai.com/v1` | — | any OpenAI-compatible endpoint |
| `insight_api_key` | `""` | server-side secret | never sent to the client |
| `insight_model` | `gpt-4o-mini` | — | model name |
| `insight_timeout_seconds` | `10.0` | 1–120 | bounded so insight never blocks the analysis flow (§24/§26) |
| `insight_retry_attempts` | `2` | 1–5 | bounded transient retries |
| `insight_max_output_tokens` | `700` | 64–4096 | output budget |

Field validators clamp every bound; misconfiguration degrades to the
deterministic provider rather than failing the app.

## 5. API & Job Changes

- **`GET /api/videos/{video_id}/insight`** → `InsightAnalysisResponse`
  (`videoId`, `status` `READY|INSUFFICIENT_DATA`, `message`, `headline`,
  `summary`, `cards[]`, `sample{collected,analyzed,skipped}`, `source`,
  `provider{name,model}`, `evidenceVersion`, `generatedAt`, `generationMs`).
  Cards carry `category` (6 kinds), title, body, and `evidence[]` lines
  (`kind`/`label`/`value`/`detail`/`topicId`).
- `JobPhase.INSIGHT` warm-up after `TOPIC` so the first UI render hits the
  memo; failure → `INSIGHT_WARM_FAILED` log, job still `COMPLETED` (§39).
- Progressive phase line maps `INSIGHT` → "building audience insight…".

## 6. Frontend UX

- New `InsightService` port + `HttpInsightService` (structural validation)
  and `UnconfiguredInsightService`; wired positionally through
  `mountApp(store, analysis, sentiment, job, topics, insight, gate)`.
- `insight` slice on the store with `begin/complete/fail/clearInsight`
  (video-id guards; reset in both `setVideoContext` branches; `changed`
  includes insight). `onInsightRetry` = `clearInsight`; `onSentimentRetry`
  also clears insight so a sentiment refresh can't pair with stale insight.
- [src/ui/components/insight.tsx](../../src/ui/components/insight.tsx):
  `InsightSection` (source badge, headline/summary, "Based on N analyzed
  comments · M skipped", hero **Why?** panel with connector-styled evidence
  chain, per-card **Why?**, topic evidence lines call
  `onTopicSelect(topicId)`), `InsightLoading` (waiting vs building copy),
  `InsightError` (+Retry), `InsightNotice` (empty).
- Topic linking: `linkedTopicId` state auto-clears after 6 s, passed to
  `TopicSection highlightTopicId` → expands the row, scrolls it into view
  (attribute-scan lookup + `scrollIntoView?.` optional-chained), and shows a
  `data-highlight` glow for 6 s; collapses whenever the slice changes.
- Technical Information disclosure gained §35 rows: insight provider,
  insight source, insight generated (timestamp · generation ms), evidence
  version (+ analyzed count). Footer shows "Sprint 7".
- ~300 lines of CSS in `overlay.css` (`.sai-card-insight`, `.sai-insight-*`,
  topic highlight glow); reduced-motion covered by the existing global rule.

## 7. Measured Performance (`backend/benchmarks/bench_insight.py`,
results in `benchmarks/results/sprint7-insight.json`, min of 5 runs, fresh
services = cold path including topic discovery)

| comments | evidence | generate | validate | **service total** | memo hit | prompt bytes | peak mem |
|---|---|---|---|---|---|---|---|
| 100 | 0.02 ms | 0.23 ms | 0.16 ms | **7.2 ms** | 0.12 ms | 407 | 0.2 MB |
| 500 | 0.02 ms | 0.23 ms | 0.16 ms | **23.5 ms** | 0.24 ms | 409 | 0.6 MB |
| 1,000 | 0.02 ms | 0.22 ms | 0.15 ms | **33.2 ms** | 0.41 ms | 411 | 0.9 MB |
| 2,500 | 0.02 ms | 0.22 ms | 0.15 ms | **98.4 ms** | 0.85 ms | 411 | 2.4 MB |
| 5,000 | 0.02 ms | 0.22 ms | 0.16 ms | **253.4 ms** | 1.61 ms | 413 | 4.0 MB |

The insight layer itself is sub-millisecond at every scale (evidence,
selection, generation, validation); the dominant cold cost is the shared
evidence build (Sprint 6 topic path + sentiment load). **Prompt size is
constant (407 → 413 bytes from 100 → 5,000 comments)** — it grows with
topics, not comments, so LLM cost is O(topics). Six cards at every scale.
Provider HTTP time is configuration-dependent and deliberately not
benchmarked (deterministic default, no network).

## 8. Tests Executed (real results)

| Suite | Command | Result |
|---|---|---|
| Backend | `cd backend && ./.venv/Scripts/python.exe -m pytest -p no:warnings --tb=no` | **469 passed, 4 skipped in 15.68s** (47 new in `tests/test_insights.py`) |
| Extension | `npm run verify` | typecheck clean, **195 passed / 14 files** (14 new in `src/ui/insight-ui.test.tsx` + 1 phase-copy test in `topics-ui.test.tsx`), build ✓ `dist/content.js` 353.19 kB |

Backend coverage: evidence building, candidate selection, grounding
(schema/word budgets/banned claims/number sets), card building + phrasing,
service layer (memo hit, LLM success, invalid-draft fallback after bounded
retries, timeout, permanent error = exactly 1 call), active-video A→B→C,
HTTP API contract + job warm-up + failure isolation, settings bounds, perf
ceilings. Frontend coverage: rendering for every state, source badges
(correct label per `source`), once-per-video loading, error + retry,
insufficient-data notice, unconfigured (no-transport) absence,
minimize/restore, Why? expansion, topic linking, collapse-on-switch, A→B
staleness guard.

## 9. Visual QA (preview harness, `vite.preview.config.ts`, port 5199)

Verified in the browser, not just tests:

- **Default scene** — full order: video → sentiment stack → **AI Audience
  Insight** card (DATA-DERIVED badge, headline, summary with grounded
  numbers, "Based on 1,211 analyzed comments · 1,218 skipped", hero Why? +
  four supporting cards each with Why?) → topics → coverage → Technical
  Information; footer "SPRINT 7".
- **Hero Why? panel** — four evidence lines with connector styling.
- **Topic link click** — target row gets `data-highlight=true` and
  `data-expanded=true`, scrolls into view, glow clears after 6 s.
- **`?scene=insight-error`** — "AUDIENCE INSIGHT UNAVAILABLE" + Retry with
  sentiment/topics intact.
- **`?scene=insight-loading`** — "BUILDING AUDIENCE INSIGHT / Connecting
  the audience signals… / Phrasing the measured analysis into a short
  briefing."
- **`?scene=insight-empty`** — INSUFFICIENT_DATA notice: "Not enough
  analyzed audience evidence to generate a reliable insight."
- **`?scene=insight-llm`** — badge reads **"AI-generated"** only for
  `source=llm`, with hint "Generated from analyzed audience data."
- **Technical Information (§35)** — open disclosure shows Insight provider
  `deterministic`, Insight source `deterministic`, Insight generated
  `2026-10-01T12:00:00Z · 4 ms`, Evidence version `1211:demo · 1,211
  analyzed`.
- **Responsive** — 1366×768: overlay docks right, insight card fits
  (335 px wide), no overflow or clipped text.

## 10. Known Limitations

- **Deterministic default is templated language** — the offline provider
  phrases the same evidence with fixed sentence patterns; it is honest and
  grounded but less varied than model output. Enabling
  `insight_provider=openai_compatible` requires a reachable endpoint and
  key; failures always degrade to deterministic text (surfaced as
  `source=fallback`, never as AI-generated).
- **LLM output can be rejected** — grounding is strict by design (§14), so
  a model that invents numbers or claim language burns its bounded retries
  and falls back; there is no human-in-the-loop repair.
- **Grounding is numeric/lexical, not semantic** — a draft whose numbers
  all exist in evidence could still mis-attribute them in prose; the banned
  -claim regex catches known over-claims only.
- **Compute-on-read** — no insight table; every cold GET after a
  fingerprint change pays the full build (253 ms at 5,000); warm hits are
  ~0.1–1.6 ms. Persisting insight was deliberately out of scope (no new
  tables).
- **Provider HTTP time is unbounded by benchmark** — timeout (default 10 s,
  max 120 s) and retry budget bound it in production, but real-network
  latency varies by endpoint.
- **Insight failure never fails the job (§39)** — a permanently failing
  insight build leaves the section in its Retry state until manually
  retried; the analysis itself is unaffected.
