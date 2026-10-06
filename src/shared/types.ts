/**
 * Shared cross-layer types.
 * Keep this file free of runtime logic so it can be imported from the content
 * script, the React UI, and future service layers.
 *
 * Sprint 2 adds the data-acquisition contract; Sprint 4 adds the sentiment
 * analysis contract - both mirror docs/13-backend-api-contract.md.
 */

/** Coarse classification of the YouTube page the user is currently on. */
export type PageKind =
  | 'watch'
  | 'shorts'
  | 'live'
  | 'home'
  | 'search'
  | 'channel'
  | 'other';

/**
 * Central representation of the active video context.
 * Only information reliably derivable from the live URL is included here;
 * metadata arrives separately through the acquisition contract below.
 */
export interface VideoContext {
  /** Full URL the context was derived from. */
  url: string;
  /** Valid 11-character YouTube video id, when the page exposes one. Never hardcoded. */
  videoId: string | null;
  pageKind: PageKind;
  /** True for supported video pages (watch/shorts/live). */
  isSupported: boolean;
  /** Epoch milliseconds when this context was captured. */
  detectedAt: number;
}

// ---------------------------------------------------------------------------
// Sprint 2: acquisition contract (camelCase JSON from the FastAPI backend)
// ---------------------------------------------------------------------------

export interface VideoStatistics {
  viewCount: number | null;
  likeCount: number | null;
  commentCount: number | null;
}

export interface VideoMetadata {
  videoId: string;
  title: string | null;
  description: string | null;
  channelId: string | null;
  channelTitle: string | null;
  publishedAt: string | null;
  categoryId: string | null;
  duration: string | null;
  statistics: VideoStatistics;
}

export interface Comment {
  commentId: string;
  videoId: string;
  author: string | null;
  text: string;
  textNormalized: string;
  publishedAt: string | null;
  updatedAt: string | null;
  likeCount: number;
  isReply: boolean;
  parentId: string | null;
}

/** ok = acquired · none = no accessible comments · disabled = comments off. */
export type CommentsStatus = 'ok' | 'none' | 'disabled';

export interface CommentCollection {
  items: Comment[];
  count: number;
  hasMore: boolean;
  status: CommentsStatus;
}

export interface AcquisitionSource {
  provider: 'youtube';
  retrievedAt: string;
  cached: boolean;
}

export interface VideoDataResponse {
  video: VideoMetadata;
  comments: CommentCollection;
  source: AcquisitionSource;
}

/** Error codes shared with the backend's error envelope. */
export type AcquisitionErrorCode =
  | 'invalid_video_id'
  | 'video_not_found'
  | 'comments_disabled'
  | 'quota_exceeded'
  | 'upstream_timeout'
  | 'upstream_unavailable'
  | 'upstream_data_invalid'
  | 'server_not_configured'
  | 'storage_unavailable'
  | 'job_cancelled'
  | 'job_interrupted'
  | 'network_error'
  | 'acquisition_failed';

export interface ApiErrorResponse {
  error: { code: string; message: string };
}

/** Per-video acquisition lifecycle held in application state. */
export type AcquisitionStatus = 'idle' | 'loading' | 'success' | 'error';

export interface AcquisitionState {
  status: AcquisitionStatus;
  /** Video this acquisition belongs to - stale results are discarded. */
  videoId: string | null;
  data: VideoDataResponse | null;
  errorCode: AcquisitionErrorCode | null;
  errorMessage: string | null;
}

// ---------------------------------------------------------------------------
// Sprint 4: sentiment analysis contract (camelCase JSON from the backend)
// ---------------------------------------------------------------------------

/** Public analysis lifecycle reported by GET /api/videos/{id}/sentiment. */
export type SentimentApiStatus =
  | 'NOT_ANALYZED'
  | 'PROCESSING'
  | 'PROCESSED'
  | 'FAILED';

export type DominantSentiment = 'POSITIVE' | 'NEUTRAL' | 'NEGATIVE';

/**
 * Aggregated verdicts for the stored dataset. Percentages come from the
 * backend's largest-remainder method (multiples of 0.1, denominator =
 * `analyzed`) and sum to exactly 100 whenever `analyzed > 0`. `skipped` =
 * analyzed rows in languages the model does not support (never folded
 * into neutral).
 */
export interface SentimentStats {
  totalComments: number;
  analyzed: number;
  skipped: number;
  positive: number;
  neutral: number;
  negative: number;
  positivePercent: number;
  neutralPercent: number;
  negativePercent: number;
}

/**
 * Dataset facts behind the analysis (Sprint 4.1). The UI must reconcile:
 *   stored == analyzed + skipped + failed + pending rows
 * `collected` equals `stored` by construction (valid comments are persisted
 * as they are acquired; rejections surface via GET /stats lastIngest).
 * `hasMore`/`limitReached` are backend evidence only - the overlay never
 * claims more comments exist without them.
 */
export interface SentimentDataset {
  collected: number;
  stored: number;
  analyzed: number;
  skipped: number;
  failed: number;
  hasMore: boolean;
  limitReached: boolean;
}

export interface SentimentAnalysis {
  videoId: string;
  status: SentimentApiStatus;
  stats: SentimentStats;
  dataset: SentimentDataset;
  /** null exactly when nothing has been analyzed. */
  dominantSentiment: DominantSentiment | null;
  /**
   * Sprint 5 audience intelligence. Optional: a pre-Sprint-5 backend body
   * without these fields must keep validating (the overlay then renders
   * only the sentiment section - never invented intelligence).
   */
  emotion?: EmotionBreakdown;
  intensity?: IntensityBreakdown;
  confidence?: ConfidenceBreakdown;
  audienceMood?: AudienceMood | null;
}

// ---------------------------------------------------------------------------
// Sprint 5: audience intelligence contract (docs/13, camelCase JSON)
// ---------------------------------------------------------------------------

/** Real NRC Emotion Lexicon categories; NEUTRAL = zero model hits. */
export type EmotionLabel =
  | 'FEAR'
  | 'ANGER'
  | 'ANTICIPATION'
  | 'TRUST'
  | 'SURPRISE'
  | 'SADNESS'
  | 'DISGUST'
  | 'JOY'
  | 'NEUTRAL';

/** Derived |sentiment| band (LOW < 0.35 <= MEDIUM < 0.70 <= HIGH). */
export type IntensityLevel = 'LOW' | 'MEDIUM' | 'HIGH';

/** Deterministic mood over real aggregates (never LLM-generated). */
export type AudienceMood =
  | 'POSITIVE'
  | 'CALM'
  | 'EXCITED'
  | 'MIXED'
  | 'CONCERNED'
  | 'NEGATIVE';

/** Count + percentage pair (percent denominators are axis-specific). */
export interface LabelShare {
  count: number;
  percent: number;
}

export interface EmotionBreakdown {
  dominant: EmotionLabel | null;
  dominantPercent: number;
  /** Full label vocabulary; counts sum to `analyzed`, percents to 100. */
  distribution: Record<EmotionLabel, LabelShare>;
}

export interface IntensityBreakdown {
  overall: IntensityLevel | null;
  distribution: Record<IntensityLevel, LabelShare>;
}

export interface ConfidenceBreakdown {
  /** Average VADER decision margin; null when nothing analyzed. */
  average: number | null;
}

/** Per-video sentiment fetch lifecycle held in application state. */
export type SentimentRequestStatus = 'idle' | 'loading' | 'success' | 'error';

export interface SentimentState {
  status: SentimentRequestStatus;
  /** Video this analysis belongs to - stale results are discarded. */
  videoId: string | null;
  data: SentimentAnalysis | null;
  errorCode: AcquisitionErrorCode | null;
  errorMessage: string | null;
}

// ---------------------------------------------------------------------------
// Sprint 6: topic / discussion / audience-preference intelligence
// (camelCase JSON, docs/13-backend-api-contract.md)
// ---------------------------------------------------------------------------

/**
 * READY = discovery ran (topics may still be empty - honest copy then);
 * INSUFFICIENT_DATA = below TOPIC_MIN_COMMENT_COUNT analyzed comments, so
 * the backend claims no themes at all (§38).
 */
export type TopicApiStatus = 'READY' | 'INSUFFICIENT_DATA';

/** Evidence-based, mutually exclusive categories (backend-gated). */
export type TopicCategory =
  | 'MOST_DISCUSSED'
  | 'APPRECIATED'
  | 'PAIN_POINT'
  | 'MIXED';

/**
 * One discovered discussion theme. Every value is aggregated from stored
 * per-comment verdicts (the topic layer never re-infers): `sentiment`
 * counts sum to exactly `mentions`, `sharePercent` uses
 * `analyzedComments` as the denominator, and a comment may belong to
 * several topics (themes are not a partition). `confidence` is topic
 * discovery confidence (support + cluster cohesion) - NOT the sentiment
 * model's decision margin. `evidence` is a factual summary, never an
 * objective claim about the video (§20/§47).
 */
export interface TopicItem {
  topicId: string;
  label: string;
  mentions: number;
  sharePercent: number;
  keyPhrases: string[];
  category: TopicCategory;
  confidence: number;
  evidence: string;
  sentiment: Record<DominantSentiment, LabelShare>;
  dominantSentiment: DominantSentiment | null;
  emotion?: EmotionBreakdown;
  intensity?: IntensityBreakdown;
}

/** GET /api/videos/{id}/topics payload. */
export interface TopicAnalysis {
  videoId: string;
  status: TopicApiStatus;
  analyzedComments: number;
  /** Honest empty/insufficient copy; null whenever topics exist. */
  message: string | null;
  topics: TopicItem[];
  mostDiscussed: TopicItem[];
  mostAppreciated: TopicItem[];
  painPoints: TopicItem[];
  mixedTopics: TopicItem[];
}

/** Per-video topic fetch lifecycle held in application state. */
export type TopicsRequestStatus = 'idle' | 'loading' | 'success' | 'error';

export interface TopicsState {
  status: TopicsRequestStatus;
  /** Video these topics belong to - stale results are discarded. */
  videoId: string | null;
  data: TopicAnalysis | null;
  errorCode: AcquisitionErrorCode | null;
  errorMessage: string | null;
}

// ---------------------------------------------------------------------------
// Sprint 7: evidence-based audience insight contract (camelCase JSON)
// ---------------------------------------------------------------------------

/**
 * READY = insight generated (LLM provider or deterministic generator);
 * INSUFFICIENT_DATA = too little analyzed evidence - honest copy, never
 * a fabricated summary (§10/§38).
 */
export type InsightApiStatus = 'READY' | 'INSUFFICIENT_DATA';

/**
 * HOW the phrasing was produced (§23): deterministic = default templates
 * over the evidence; llm = validated model output; fallback = model was
 * configured but failed, deterministic text served instead. The UI must
 * never present deterministic/fallback text as AI-generated (§45).
 */
export type InsightSource = 'deterministic' | 'llm' | 'fallback';

export type InsightCardCategory =
  | 'OVERALL_REACTION'
  | 'WHAT_WORKED'
  | 'MAIN_DISCUSSION'
  | 'PAIN_POINT'
  | 'EMOTIONAL_SIGNAL'
  | 'TAKEAWAY';

export type InsightEvidenceKind =
  | 'SENTIMENT'
  | 'EMOTION'
  | 'INTENSITY'
  | 'CONFIDENCE'
  | 'TOPIC'
  | 'APPRECIATED'
  | 'PAIN_POINT'
  | 'MIXED'
  | 'SAMPLE';

/** One inspectable evidence line behind an insight (§20 "Why?" panels). */
export interface InsightEvidenceLine {
  kind: InsightEvidenceKind;
  label: string;
  value: string;
  detail: string | null;
  /** Links the line to its topic card for insight -> data navigation (§21). */
  topicId: string | null;
}

/** One expandable insight card (§19) with real, linked evidence (§20). */
export interface InsightCard {
  category: InsightCardCategory;
  title: string;
  body: string;
  evidence: InsightEvidenceLine[];
}

/** Denominator honesty (§11): conclusions rest on `analyzed`. */
export interface InsightSample {
  collected: number;
  analyzed: number;
  skipped: number;
}

/** Generation provenance for Technical Information (§35) - not the main UI. */
export interface InsightProviderInfo {
  name: string;
  model: string | null;
}

/** GET /api/videos/{id}/insight payload. */
export interface InsightAnalysis {
  videoId: string;
  status: InsightApiStatus;
  /** Honest insufficient-data copy; null whenever READY. */
  message: string | null;
  headline: string;
  summary: string;
  cards: InsightCard[];
  sample: InsightSample;
  source: InsightSource;
  provider: InsightProviderInfo;
  evidenceVersion: string;
  generatedAt: string | null;
  generationMs: number;
}

export type InsightRequestStatus = 'idle' | 'loading' | 'success' | 'error';

export interface InsightState {
  status: InsightRequestStatus;
  /** Video this insight belongs to - stale results are discarded. */
  videoId: string | null;
  data: InsightAnalysis | null;
  errorCode: AcquisitionErrorCode | null;
  errorMessage: string | null;
}

// ---------------------------------------------------------------------------
// Sprint 4.3: background analysis job contract (camelCase JSON, docs/13)
// ---------------------------------------------------------------------------

/**
 * Background job lifecycle. COMPLETED/FAILED are the happy/error ends;
 * CANCELLED = superseded by a different active video (Sprint 4.2),
 * STALE = interrupted by a backend restart - both terminal + retryable.
 */
export type AnalysisJobStatusValue =
  | 'QUEUED'
  | 'ACQUIRING'
  | 'ANALYZING'
  | 'COMPLETED'
  | 'FAILED'
  | 'CANCELLED'
  | 'STALE';

export type AnalysisJobPhase =
  | 'NONE'
  | 'ACQUISITION'
  | 'SENTIMENT'
  | 'TOPIC'
  | 'INSIGHT'
  | 'COMPLETE';

/** POST /api/videos/{id}/analysis - 202 Accepted payload (§9). */
export interface AnalysisJobCreated {
  jobId: string;
  videoId: string;
  status: AnalysisJobStatusValue;
}

/**
 * GET /api/videos/{id}/analysis/status payload (§7/§10). Every count is
 * backend-derived and REAL: the overlay must never draw a percentage the
 * backend did not compute.
 */
export interface AnalysisJob {
  jobId: string;
  videoId: string;
  status: AnalysisJobStatusValue;
  phase: AnalysisJobPhase;
  collected: number;
  stored: number;
  analyzable: number;
  analyzed: number;
  skipped: number;
  failed: number;
  pending: number;
  hasMore: boolean;
  errorCode: string | null;
  errorMessage: string | null;
  createdAt: string;
  updatedAt: string;
  finishedAt: string | null;
}

/** Per-video background job lifecycle held in application state. */
export type JobRequestStatus = 'idle' | 'running' | 'error';

export interface JobState {
  status: JobRequestStatus;
  /** Video this job belongs to - stale snapshots are discarded. */
  videoId: string | null;
  jobId: string | null;
  /** Latest polled snapshot (progress); null until the first poll lands. */
  job: AnalysisJob | null;
  errorCode: AcquisitionErrorCode | null;
  errorMessage: string | null;
}

// ---------------------------------------------------------------------------
// Sprint 8: realtime audience intelligence contract (GET .../realtime)
// ---------------------------------------------------------------------------

/** Net-balance movement against the backend's percentage-point floor. */
export type RealtimeTrendState = 'RISING' | 'FALLING' | 'STABLE';

/** Audience activity band (comments-per-minute over the trailing window). */
export type RealtimeActivityLevel = 'LOW' | 'MODERATE' | 'HIGH';

export interface RealtimeSentimentBreakdown {
  positive: number;
  neutral: number;
  negative: number;
}

/**
 * Trend since the previous analyzed-batch snapshot. The arrow is always
 * paired with a text label + exact delta (never color-only, §21).
 */
export interface RealtimeTrend {
  state: RealtimeTrendState;
  changePp: number;
  positivePp: number;
  neutralPp: number;
  negativePp: number;
}

export interface RealtimeActivity {
  level: RealtimeActivityLevel;
  newRecent: number;
  windowMinutes: number;
  ratePerMinute: number;
}

/**
 * GET /api/videos/{videoId}/realtime payload (Sprint 8 §19).
 * Counts are backend-live; `version` flips iff (stored, analyzed) changed
 * and is the overlay's cheap silent-refetch trigger. `newComments` counts
 * the comments acquired by the most recent data update.
 */
export interface RealtimeStatus {
  videoId: string;
  enabled: boolean;
  monitoring: boolean;
  pollIntervalSeconds: number;
  lastCheckedAt: string | null;
  lastUpdatedAt: string | null;
  newComments: number;
  totalComments: number;
  analyzed: number;
  pending: number;
  skipped: number;
  failed: number;
  sentiment: RealtimeSentimentBreakdown;
  trend: RealtimeTrend;
  activity: RealtimeActivity;
  dominantEmotion: string | null;
  version: string;
}

export type RealtimeRequestStatus = 'idle' | 'success' | 'error';

export interface RealtimeState {
  status: RealtimeRequestStatus;
  /** Video this status belongs to - stale polls are discarded. */
  videoId: string | null;
  data: RealtimeStatus | null;
  errorCode: AcquisitionErrorCode | null;
  errorMessage: string | null;
}

// ---------------------------------------------------------------------------
// Overlay state machine
// ---------------------------------------------------------------------------

/**
 * Overlay lifecycle states.
 * `closed | open | ready` shell (Sprint 1); `loading | complete | error` are
 * acquisition states (Sprint 2); `analyzing` is the sentiment request window
 * (Sprint 4) between acquisition COMPLETE and the final COMPLETE. UI label
 * for `complete` is "DATA ACQUIRED".
 * `connecting` (Sprint 4.4 §15/§19) covers the bounded backend-availability
 * gate shown before analysis traffic starts.
 *
 * CLOSED → OPEN → READY → (CONNECTING →) LOADING → COMPLETE ⇄ ANALYZING →
 * COMPLETE | ERROR
 */
export type OverlayStatus =
  | 'closed'
  | 'open'
  | 'ready'
  | 'connecting'
  | 'loading'
  | 'analyzing'
  | 'complete'
  | 'error';

/**
 * Frontend-only console section (multi-section redesign). Pure
 * presentation state: it never fetches, computes or stores data - each
 * section renders the SAME store-driven values, just in a focused view.
 */
export type SectionId = 'overview' | 'sentiment' | 'emotion' | 'topics' | 'insight';

/** Snapshot of application state consumed by the React UI. */
export interface OverlayState {
  status: OverlayStatus;
  /** Orthogonal to status: minimizing preserves the underlying status. */
  minimized: boolean;
  /**
   * Active console section (UI-only). Defaults to `overview`, survives
   * minimize/restore, and resets to `overview` whenever the active video
   * changes so a new video never opens on another video's section.
   */
  activeSection: SectionId;
  videoContext: VideoContext | null;
  /** Transient user-facing message (never raw stack traces). */
  notice: string | null;
  /** Current acquisition for the current video (cleared on video change). */
  acquisition: AcquisitionState;
  /** Current sentiment analysis for the current video (cleared on video change). */
  sentiment: SentimentState;
  /** Current topic intelligence for the current video (Sprint 6; cleared on video change). */
  topics: TopicsState;
  /** Current audience insight for the current video (Sprint 7; cleared on video change). */
  insight: InsightState;
  /** Current background analysis job (Sprint 4.3; cleared on video change). */
  job: JobState;
  /** Realtime monitor status for the current video (Sprint 8; cleared on video change). */
  realtime: RealtimeState;
}

/** Message contract shared with the (future) background worker. */
export type ExtensionMessage = { type: 'PING' };
export type ExtensionMessageResponse = { pong: true };
