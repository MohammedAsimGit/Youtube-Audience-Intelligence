# Decision Record — YouTube Data Acquisition (Sprint 2)

Format mirrors the TDR convention (need · alternatives · advantages ·
limitations · fit · deployment · maintenance).

## AD-01 — Official YouTube Data API v3 (only)

**Status: DECIDED (implements TDR-05)**

1. *Needed:* permitted video metadata + comment text as the real data layer.
2. *Alternatives:* HTML scraping (rejected: ToS + fragility), unofficial
   comment APIs (rejected: unstable, untrusted), third-party aggregators
   (rejected: external dependency + cost + privacy surface), browser-DOM
   scraping from the content script (rejected: couples client to page markup,
   violates client/intelligence separation).
3. *Advantages:* documented contract, quota-protection semantics, stable
   reasons for error states, Google-supported.
4. *Limitations:* 10,000 units/day default quota; `commentThreads` paging;
   `commentsDisabled` is a hard API error (handled as explicit UI state).
5. *Fit:* backend-only key custody satisfies the security rules; ids from the
   extension's URL parser avoid the expensive `search.list` (100 units).
6. *Deployment:* key in `backend/.env` (git-ignored) → pydantic-settings;
   rotate via env only.
7. *Maintenance:* facts re-verified each sprint against Google's docs
   (see docs/12 for the verified table).

## AD-02 — Single-gateway acquisition endpoint

**Status: DECIDED** — `GET /api/videos/{video_id}` returns the full
acquisition bundle (metadata + comments + source info) in one round trip.

- *Why not* separate metadata/comments endpoints? Sprint 2's overlay always
  needs both together; one call = fewer quota-accounting edge cases, simpler
  contract, simpler caching key. Split later only if a real use case appears.
- *Why not* proxy raw YouTube responses? The brief forbids exposing raw
  upstream shape; normalization at the gateway decouples every consumer from
  Google's field quirks (string counters, thread nesting).

## AD-03 — Bounded, page-capped comment crawl

**Status: DECIDED (revised in Sprint 4.1)** — page size and dataset limit
are now two distinct settings:

- `MAX_COMMENTS_PER_REQUEST` = **page size** (default **100**, YouTube's
  per-request maximum; `maxResults = min(pageSize, remaining)`).
- `COMMENT_ACQUISITION_MAX_COMMENTS` = **dataset limit per video per
  acquisition run** (default **5000**), with `MAX_API_PAGES` (100) as the
  pathological-token safety bound.

Rationale for the revision: conflating page size with the dataset limit
left an effective 50-comment ceiling on every video. Boundedness is
preserved (limit × page bound, never unbounded), quota stays predictable
(1 unit/page), and `comments.hasMore` still reports truncation — only when
YouTube actually offered a next page.

## AD-04 — `commentsDisabled` is a state, not an error

**Status: DECIDED** — Google returns `403 / commentsDisabled`; we return
**200** with `comments.status = "disabled"` and full metadata.

- Rationale: the video data is valid and useful; collapsing this into an
  error would hide metadata behind a false failure. The overlay renders the
  dedicated COMMENTS UNAVAILABLE block (brief §25).
- *Verified:* Google errors reference lists `forbidden (403), commentsDisabled`.

## AD-05 — In-memory TTL/LRU cache (seam, not final)

**Status: DECIDED for Sprint 2 (supersedes nothing in TDR-07)**

- *Need:* repeated Analyze clicks must not burn quota (brief §26/§27).
- *Choice:* process-local TTL (600s) + LRU (128) behind a `cache.get/set`
  seam — zero new dependencies, testable with an injected clock.
- *Explicitly not* the production cache: no persistence, no sharing across
  instances. Persistent storage arrives with Sprint 3 (TDR-06/TDR-07).

## AD-06 — camelCase stable internal contract

**Status: DECIDED** — Pydantic models serialize via camelCase aliases;
TypeScript mirrors them exactly in `src/shared/types.ts`; both sides tested.

- *Why:* the extension is TypeScript (natural camelCase), and a stable
  project-owned contract (not Google's shape) is what the brief's
  normalization rule requires. Contract doc: `docs/13-backend-api-contract.md`.

## AD-07 — Extension talks to loopback with host_permissions

**Status: DECIDED** — `host_permissions: http://127.0.0.1/*, http://localhost/*`.

- *Why needed:* MV3 content-script fetches to a different origin are subject
  to page CORS; the loopback host permission exempts them reliably (Chrome
  treats them as extension-privileged).
- *Why safe:* loopback only — no web hosts, no YouTube host permission
  (YouTube access comes solely from `content_scripts.matches`), no optional
  permissions. Backend CORS (exact origins + extension-id regex, never `*`)
  remains as an independent second layer, verified live.

## AD-08 — Sync httpx client, app-factory testability

**Status: DECIDED for Sprint 2**

- `httpx.Client` (sync) runs in FastAPI's threadpool — adequate for ≤3
  upstream calls/request at MVP volume; async conversion is a measured-need
  change, not a rewrite (`YouTubeClient` surface is already isolated).
- `create_app(settings=…, youtube_client=…)` dependency injection lets all 21
  tests run offline against a fake client (mock policy: clearly labeled test
  fixtures, never presented as real data).
