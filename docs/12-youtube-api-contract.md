# 12 — YouTube Data API Contract (as used)

**Rule followed:** official YouTube Data API v3 only — no scraping, no
unofficial comment APIs, no invented endpoints. Facts below were verified
against Google's official documentation (`developers.google.com/youtube/v3`)
before implementation (Sprint 0 Rule 3).

## Endpoints Used

### 1. `GET https://www.googleapis.com/youtube/v3/videos`

| Param | Value | Why |
|---|---|---|
| `part` | `snippet,statistics` | Only fields the product needs (§12 of brief) |
| `id` | validated 11-char video id | Never a search — 1 quota unit |
| `key` | server-side `YOUTUBE_API_KEY` | Sent as a query param by httpx; **never logged** |

**Quota:** 1 unit/request.

**Fields consumed → internal mapping:**

| YouTube field | Internal field | Notes |
|---|---|---|
| `items[].id` | `video.videoId` | |
| `snippet.title` | `video.title` | missing → `null` (never invented) |
| `snippet.description` | `video.description` | |
| `snippet.channelId` / `channelTitle` | `video.channelId` / `channelTitle` | |
| `snippet.publishedAt` | `video.publishedAt` | ISO-8601 → datetime, bad → `null` |
| `snippet.categoryId` | `video.categoryId` | |
| `statistics.viewCount/likeCount/commentCount` | `statistics.*` | **arrive as JSON strings** → `parse_int`, garbage → `null` |
| — | `video.duration` | `null` in Sprint 2 (duration lives in `contentDetails.part`, a deliberate later addition if needed) |

Empty `items` (unknown/deleted id) → internal `VideoNotFound` (404). HTTP 404
from Google maps to the same internal error.

### 2. `GET https://www.googleapis.com/youtube/v3/commentThreads`

| Param | Value | Why |
|---|---|---|
| `part` | `snippet,replies` | top-level comments + their replies |
| `videoId` | validated id | |
| `maxResults` | `min(100, remaining_cap)` | API max is 100/page |
| `pageToken` | from previous `nextPageToken` | pagination |
| `order` | `time` | newest-first acquisition (deterministic for a crawl window) |
| `key` | server-side | never logged |

**Quota:** 1 unit/page.

**Pagination algorithm (bounded — never unbounded download; Sprint 4.1):**

```text
# page size ≠ dataset limit (two independent settings)
cap      = COMMENT_ACQUISITION_MAX_COMMENTS   # default 5000 per video per run
pageSize = MAX_COMMENTS_PER_REQUEST          # default 100 (YouTube page max)
collected, pageToken, seenTokens = [], None, set()
while collected < cap and pages < MAX_API_PAGES:
    page = commentThreads(maxResults=min(pageSize, cap - collected),
                          pageToken=pageToken)
    batch = normalize(page)[:cap - collected]           # threads → flat comments
    persist(batch)                                      # page → batch → SQLite → next page
    collected += len(batch)
    log "comment page retrieved" (page#, comments, units=1, storage_ok)
    pageToken = page.nextPageToken
    if not pageToken: break                             # YouTube ran out
    if pageToken in seenTokens:                         # loop guard
        log "PAGINATION_TOKEN_LOOP"; hasMore = False; break
    seenTokens.add(pageToken)
hasMore = token remained when we stopped                # limit/page bound hit
```

**Thread vs individual comment (§15):** one API item = one thread.
Normalization flattens:
- `snippet.topLevelComment` → `Comment(isReply=false, parentId=null)`
- each `replies.comments[]` → `Comment(isReply=true, parentId=topId)`

Thread items without a top-level comment or with empty/whitespace text are
**skipped** (logged counts, never crash, never fabricated).

## Verified Error Reasons (Google error reference)

| HTTP | `errors[].reason` | Internal mapping |
|---|---|---|
| 403 | `commentsDisabled` | **not an error**: `comments.status="disabled"` + metadata still served |
| 403 | `quotaExceeded` | `QuotaExceeded` → 429 |
| — | `dailyLimitExceeded` | `QuotaExceeded` → 429 |
| 400 | `keyInvalid` | `MissingApiKey` → 503 `server_not_configured` |
| 404 | — | `VideoNotFound` → 404 |
| other ≥400 | any/unmapped | `UpstreamUnavailable` → 502 (reasons logged server-side only) |
| non-JSON body / schema failure | — | `UpstreamUnstable` → 502 |

Error payloads are parsed defensively (`errors[].reason` may be absent —
unmapped reasons degrade to 502, never raise a different internal type).

## Deliberately NOT Used

| Endpoint | Why not |
|---|---|
| `search.list` (100 units!) | Video ids arrive from the extension's URL parsing — 50× too expensive |
| `videos.list` with `part=contentDetails` | Duration not needed for Sprint 2 |
| Scraping / unofficial APIs | ToS + stability — explicitly forbidden |
| Any write endpoint | Read-only product |

## Quota Model Summary

- Default project quota: **10,000 units/day** (resets midnight Pacific).
- One acquisition of ≤50 comments = **2 units** (1 metadata + 1 thread page).
- Cache hits = **0 units** (TTL 600s default).
- Exhaustion surfaces as an honest `QUOTA EXCEEDED` overlay state; no bypass
  attempts, no silent fake data.

## Response Shape Notes

- Counters are **strings** in Google's JSON → validated loosely
  (`str | None`) and coerced during normalization; malformed values → `null`.
- Extra/unknown JSON fields are ignored (`extra="ignore"`), never fatal.
- Missing optional fields → `null` / default; **never invented values**.
