# 06 — Non-Functional Requirements

## Performance

- **Responsive UI** — overlay interactions (open/close/minimize) feel immediate;
  no layout jank; extension must not noticeably degrade normal YouTube browsing.
- **Minimal extension overhead** — no unnecessary polling, no expensive DOM
  observers, no continuous animation loops, no oversized client dependencies.
- **Asynchronous processing** — long-running analysis never blocks the browser;
  requests return job handles and the UI polls/subscribes for completion.
- **Efficient data processing** — pipeline stages stream/page data; avoid loading
  entire comment collections into memory where unnecessary.
- **Cache utilization** — repeat analyses served from cache; measure cache hit rate.
- **Measured, not claimed** — latency/throughput numbers are reported only after
  actual measurement (see [10-success-criteria.md](10-success-criteria.md)).

## Scalability

- Designed to process **increasing volumes of unstructured comments** efficiently
  (candidate evaluation sizes: 1,000 / 10,000 / 50,000 / 100,000+ comments — actual
  sizes depend on API availability, quota, infrastructure, and resources).
- Increasing numbers of analyzed videos must not degrade lookup (indexing by video
  ID, analysis status, timestamps).
- Backend architecture must be extensible to more platforms later without client
  rewrites (platform-agnostic API contract).
- **No scale is claimed unless actually tested.**

## Reliability

Every external dependency must have a designed failure state:

| Failure | Required behavior |
|---|---|
| Comments disabled on video | Honest "no comments available" state; no fake numbers |
| Video unavailable / deleted | Clear error state in overlay |
| Insufficient comments | Partial/low-confidence indication or honest refusal to analyze |
| API quota limit exhausted | Queue or report degradation; never silently fabricate |
| Network failure | Retry with backoff where appropriate; visible failure state |
| Malformed API/backend response | Validate and reject; log for developers; friendly message for users |
| Unsupported content/language | Graceful "not supported" state |
| Analysis timeout | Job marked failed; UI shows timeout state |
| AI model failure | Job failed; no partial results presented as complete |
| Database failure | API error response; overlay error state |
| Cache failure | Degrade to fresh-analysis path or honest error — never crash |

Raw stack traces must never be shown to users; developer logging is allowed.

## Maintainability

- Modular architecture with clear layer boundaries (extension / API / pipeline / AI).
- Documented interfaces and Technology Decision Records for every major choice.
- Reusable services: UI never talks directly to platform APIs; one analysis-service
  seam between UI and backend.
- Consistent, switchable logging across layers.
- Documentation lives in-repo and is updated each sprint.

## Security

- **API keys must never appear in extension code, manifest, or client bundles.**
- Secrets remain server-side (environment variables / secret management).
- Extension permissions minimized: YouTube-only host access, no broad permissions
  unless technically justified (each permission documented).
- Backend inputs validated (video IDs, pagination tokens, request bodies).
- API endpoints get appropriate rate limiting and abuse protection.
- External services treated as untrusted: validate everything received.
- No `eval`, no remote script injection, no unnecessary remote code.
- No unnecessary user information collected.

## Privacy (Data Minimization)

| Data | Collected? | Why | Retention |
|---|---|---|---|
| Public video metadata (id, title, channel) | Yes (backend) | Identify & label analysis | Cached with analysis; revisable |
| Public comments (text, timestamps, likes) | Yes (backend) | The analysis input | Defined in DB sprint; raw retention minimized & documented |
| YouTube API key | Yes (server only) | API access | Secret store; rotated as needed |
| End-user identity / accounts | **No** | Not required for MVP | — |
| User-specific profiles | **No** | Data minimization | — |
| Browsing history outside YouTube analysis context | **No** | Only current video context is read | — |
| Association of users with analyses | **No** | Analyses are per-video, not per-user | — |

Open questions to confirm in the data sprint: whether raw comments are persisted
long-term or pruned after aggregation; whether any external AI service receives
comment text (if so, documented here and disclosed). Anything transmitted between
components is listed in [architecture/data-flow.md](architecture/data-flow.md).

## Accessibility

- Full keyboard interaction: every control reachable and operable, visible focus,
  sensible focus management on open/close.
- Meaningful accessible names for buttons (open, close, minimize, analyze).
- Readable typography and sufficient text contrast in the futuristic theme.
- `prefers-reduced-motion` respected: animations reduced or removed.
- Status changes announced politely (`aria-live`) where relevant.

## Compatibility

- Desktop Chrome/Chromium as primary target; test at 1280×720, 1366×768, 1440×900,
  1920×1080.
- Manifest V3 compliance with current Chrome extension requirements.
