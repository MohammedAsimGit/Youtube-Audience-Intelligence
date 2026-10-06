# 10 — Success Criteria & Evaluation Plan

Evaluation happens across four dimensions. **No target may be claimed as achieved
until it has actually been measured.**

## 1. AI Evaluation

| Capability | Metrics | Notes |
|---|---|---|
| Sentiment classification | accuracy, precision, recall, F1 | per-class (positive/neutral/negative) and macro/micro averages |
| Emotion classification | accuracy, precision, recall, F1 | taxonomy fixed when the model is chosen |
| Topic extraction | coherence / human review; precision@k vs. judged topics | method documented in the AI sprint |
| Aspect extraction + aspect sentiment | precision, recall, F1 | against a hand-labeled sample |

- The actual dataset, labeling approach, and methodology are documented **in the AI
  sprint** — not before.
- Model choice follows the model-selection rule in
  [architecture/ai-analysis-spec.md](architecture/ai-analysis-spec.md): evaluate
  candidates first, commit later.
- **AI must remain evidence-grounded:** spot-check summaries for unsupported claims;
  any claim not traceable to analyzed data is a defect.

## 2. System Evaluation

| Dimension | What is measured |
|---|---|
| API latency | p50/p95 response times for analysis request & result retrieval |
| Analysis latency | end-to-end job duration (collection → result stored) by dataset size |
| Throughput | comments processed per unit time through the pipeline |
| Cache effectiveness | hit rate, and latency/cost avoided per hit (incl. saved API quota) |
| Failure rate | failed jobs / total jobs, by failure category |
| Scaling behavior | processing time at multiple dataset sizes (e.g., 1k / 10k / 50k / 100k+ comments — actual sizes depend on quota, infrastructure, and resources) |

Measurement environment and method are documented alongside results.

## 3. UI Evaluation

| Dimension | Criteria |
|---|---|
| Responsiveness | open/close/minimize feel immediate; no layout jank; no measurable degradation of normal YouTube browsing |
| Readability | text legible over video/page at all target resolutions (1280×720 … 1920×1080) |
| Overlay obstruction | essential YouTube controls remain usable; video remains visible under the glass layer |
| Animation performance | no continuous heavy animation; reduced-motion respected |
| Accessibility | keyboard-reachable controls, visible focus, sufficient contrast, labeled buttons |
| Browser compatibility | current Chrome/Chromium; Manifest V3 compliant |

## 4. User Evaluation

Potentially evaluate whether users can understand audience reactions **more
efficiently** than manually inspecting comments (task-completion time and
comprehension questions, with or without the overlay).

- Only claims supported by an actual study may be reported.
- Without a formal study, results are reported as qualitative feedback only.

## Overall Success Criteria (MVP)

The project succeeds when, on real permitted data:

1. A user on a YouTube watch page can activate the overlay and receive **real**
   analysis (sentiment, emotions, topics, aspects, summary) without leaving YouTube.
2. Navigating between videos updates context and never displays stale results.
3. Every displayed statistic is traceable to collected comments — zero fabricated
   production intelligence.
4. Failure modes (comments disabled, quota exhausted, network/backend down) surface
   as honest states, never as invented numbers.
5. The pipeline's processing time is **measured across at least two dataset sizes**,
   demonstrating behavior as volume grows.
6. The extension requests no unnecessary permissions, ships no secrets, and does
   not interfere with YouTube's core experience.
7. All architecture, decisions, and evaluation results are documented in this
   repository.

## Reporting Rules

- State measurement conditions (dataset, environment, date) with every number.
- Distinguish *measured* results from *targets*.
- Label any illustrative UI values as `DEMO DATA`.
