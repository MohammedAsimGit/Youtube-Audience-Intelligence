# 02 — Problem Statement

## Informal Problem

Modern social-media platforms generate enormous quantities of unstructured
user-generated data. A single popular YouTube video may receive a very large number
of comments containing:

- opinions, praise, criticism
- questions and requests
- emotions and reactions
- complaints and concerns
- recommendations, comparisons
- personal experiences

Manually reading and understanding this information becomes increasingly difficult as
volume grows. Traditional engagement metrics — views, likes, shares, comment counts —
are **quantitative**: they say how *much* engagement happened, not the qualitative
nature of the audience reaction. They cannot answer:

- Are viewers reacting positively or negatively, and in what proportion?
- What emotions dominate the discussion?
- What topics recur across thousands of comments?
- Which specific **aspects** (price, design, battery, quality…) are praised or criticized?
- **Why** does the audience feel the way it does?

## Formal Problem Statement

> Social-media platforms generate large volumes of unstructured textual feedback
> through user comments and discussions. Traditional engagement metrics provide
> limited insight into the collective opinions and reasons behind audience
> reactions. Manually analyzing thousands of comments is time-consuming and
> impractical. There is therefore a need for a scalable Database/Big Data and
> AI-driven system capable of collecting, processing, analyzing, aggregating, and
> presenting large-scale social-media feedback as meaningful sentiment intelligence
> directly within the user's content-consumption environment.

*(Final wording may be refined during Sprint 0 review; the Database / Big Data
component remains central.)*

## Why This Is a Database / Big Data Problem

| Big Data concern | How it appears in this project |
|---|---|
| **Volume** | Comment collections from popular videos reach tens of thousands of entries per video, accumulating across analyzed videos. |
| **Velocity** | New comments arrive continuously; analyses must be reusable and refreshable, not one-off. |
| **Variability** | Comments are messy: emojis, URLs, mentions, slang, multilingual text, spam, duplicates, malformed content. |
| **Unstructured → structured** | Free text must become structured, queryable intelligence: labels, distributions, topic lists, aspect-sentiment pairs. |
| **Ingestion** | Comments arrive paged from an external API and must be moved reliably into a processing pipeline. |
| **Storage & indexing** | Raw and processed data must be stored with indexing that supports lookup by video, topic, aspect, and time. |
| **Aggregation** | Comment-level predictions must be folded into distributions, rankings, and patterns. |
| **Caching** | Repeated analysis of the same video must be served from cache, avoiding re-collection and re-inference. |
| **Asynchronous processing** | Long-running pipelines must not block the user; work happens in background jobs. |
| **Performance** | Pipeline throughput must be measured across dataset sizes, not assumed. |

## What "Solving It" Looks Like

A user opens a YouTube video, clicks the floating AI button, and within the overlay
sees — over the video, without leaving the page:

```text
Positive: 72%   Neutral: 19%   Negative: 9%        ← real, analyzed data
Dominant emotions: Joy, Surprise
Top topics: performance, price, camera quality
Aspects:  battery → positive      price → negative
AI summary: grounded strictly in the analyzed comments
```

Every number traceable to real collected data. No fabricated intelligence, ever
(product principle 6).

## Out of Problem Scope

- Personalized user profiling or identity analysis (privacy: data minimization).
- Cross-platform social listening (future scope, see [09-future-scope.md](09-future-scope.md)).
- Real-time streaming analysis (future scope).
