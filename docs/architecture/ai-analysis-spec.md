# AI Analysis Specification

Sprint 0 definition of AI capability. **No model is permanently selected here**
(model-selection rule below), and no AI code exists yet.

## Layer 1 — Sentiment

Initial categories:

```text
Positive   Neutral   Negative
```

Potential outputs per category: `count`, `percentage`, `confidence`.
Deliverable: whole-comment classification plus the aggregated distribution.

## Layer 2 — Emotion

Potential categories:

```text
Joy   Anger   Sadness   Fear   Disgust   Surprise   Neutral
```

The final taxonomy must be selected based on the chosen model/dataset and
documented in the AI sprint.

## Layer 3 — Topic Extraction

Identify recurring discussion topics discovered **from actual data**
(e.g., performance, price, design, battery, camera, quality, features — examples
only, never hard-coded outputs). Deliverable: ranked topics with frequency and
representative evidence.

## Layer 4 — Aspect-Based Sentiment Analysis

Determine `Aspect → Sentiment` (e.g., `Battery → Positive`, `Price → Negative` —
illustrative only). Aspects are extracted from text, not from a fixed list.
Deliverable: aspect pairs with support counts and confidence.

## Layer 5 — Key Opinions

Identify representative themes and opinions **supported by the analyzed data**
(comment evidence references required).

## Layer 6 — AI Insight

Generate a concise summary based on aggregated evidence.

**Hard rule:** the summary must not introduce unsupported claims (Principle 4).
Every sentence must be traceable to analyzed comments/statistics.

## Model Selection Rule (Sprint 0 does NOT lock a model)

Do not prematurely commit to BERT, RoBERTa, DistilBERT, GPT-class LLMs, Llama,
Gemma, or any other family. For every candidate, document:

1. expected strengths,
2. expected limitations,
3. computational requirements,
4. inference cost (latency + resource use at expected comment volumes),
5. language support,
6. evaluation methodology (metrics + dataset from
   [../10-success-criteria.md](../10-success-criteria.md#1-ai-evaluation)).

Candidates to evaluate (not selections): HuggingFace Transformers models
(classification heads), scikit-learn baselines, spaCy/NLTK preprocessing,
prompted LLMs. Final selection occurs in the AI/ML sprint with measured results.

**Baseline first:** a simple, transparent baseline (e.g., lexicon or classic ML)
is evaluated alongside neural candidates so accuracy-vs-cost trade-offs are real
numbers, not assumptions.

## Preliminary Analysis Contract (PLANNING ONLY)

Conceptual backend response shape for discussion — **do not implement blindly**;
the final contract is validated in the backend/API sprint.

```json
{
  "video": {
    "id": "",
    "title": "",
    "channel": ""
  },
  "analysis": {
    "commentsAnalyzed": 0,
    "sentiment": {
      "positive": 0,
      "neutral": 0,
      "negative": 0
    },
    "emotions": [],
    "topics": [],
    "aspects": [],
    "keyOpinions": [],
    "summary": ""
  },
  "metadata": {
    "status": "",
    "analyzedAt": "",
    "dataCoverage": 0
  }
}
```

Open contract questions for the API sprint: confidence fields, evidence
references per insight, model/version provenance, partial-result semantics,
`dataCoverage` definition (comments analyzed ÷ comments available), and error
envelope shape. The contract must remain platform-agnostic (usable by a future
Android client).

## Integrity Rules

- Percentages and counts derive only from actually analyzed comments.
- `commentsAnalyzed` and `dataCoverage` disclose coverage honestly (partial
  analysis is labeled, never dressed as complete).
- If real analysis cannot be performed, the API returns an error/empty result —
  **never fabricated numbers** (FR-12, Principle 6).
