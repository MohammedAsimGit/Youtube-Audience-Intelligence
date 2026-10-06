# 04 — User Personas and Use Cases

## Primary Persona: The Digital Content Consumer

> A person who wants to quickly understand audience reaction to a YouTube video
> without manually reading hundreds or thousands of comments.

| Attribute | Description |
|---|---|
| Context | Watches YouTube regularly on desktop Chrome/Chromium. |
| Goal | Know *what the audience thinks* about the current video — quickly, in place. |
| Frustration | Comment sections are too long to read; sorted-by-top gives a skewed, partial view; engagement numbers don't explain anything. |
| Behavior | Expects tools to appear where they already are; abandons workflows that require copying URLs into separate websites. |
| Success moment | Opens the overlay and immediately sees sentiment distribution, dominant emotions, recurring topics, aspect verdicts, and a short grounded summary. |

The architecture must **not** be restricted to a single use case; the persona is a
center of gravity, not a cage.

## Secondary perspectives (same architecture, same MVP)

- **Content creator / researcher** — wants a fast read on what viewers praise or
  criticize across a video's discussion.
- **Student / educator** — gauges reception of educational content at a glance.
- **Buyer / evaluator** — reads collective reaction to a product-review video
  before forming an opinion.

No user accounts or identity tracking are required for any perspective
(data minimization — see [06-non-functional-requirements.md](06-non-functional-requirements.md)).

## Core Use Cases

| # | Use case | Trigger | Expected behavior |
|---|---|---|---|
| UC-1 | Understand a product review's reception | User opens a product review video, activates the AI button | Overlay shows sentiment split, praised/criticized aspects (e.g., battery → positive, price → negative), top topics, grounded summary |
| UC-2 | Gauge reaction to educational content | User watches a lecture/tutorial | Overlay summarizes what viewers found clear, confusing, or thankful for |
| UC-3 | Evaluate audience response to entertainment | Movie clip, trailer, music video | Dominant emotions and discussion themes surfaced without reading comments |
| UC-4 | Identify common complaints | Any video | Negative-sentiment clusters and criticized aspects rise to the top |
| UC-5 | Identify appreciated features | Any video | Positive topics/aspects listed with support from analyzed data |
| UC-6 | Discover discussion themes | Curious viewer | Topic list derived from actual comments, not hard-coded |
| UC-7 | Surface audience concerns | Comment section too large | Recurring concerns aggregated into key opinions |
| UC-8 | Summarize a large comment section | Impatient reader | Concise AI summary strictly grounded in analyzed comments |
| UC-9 | Navigate to another video | SPA navigation within YouTube | Context re-detects; stale analysis is never shown for the new video; cache consulted, analysis triggered if needed |

## Non-Goals for Users

- No requirement to create accounts or log in for the MVP.
- No requirement to leave YouTube at any point.
- No expectation of results when real analysis cannot be performed — failure states
  are shown honestly; results are never fabricated.

## Future (Not MVP) User Value

Cross-video comparison, historical tracking, creator analytics, brand sentiment,
trend detection, multilingual analysis, personalized insights — see
[09-future-scope.md](09-future-scope.md).
