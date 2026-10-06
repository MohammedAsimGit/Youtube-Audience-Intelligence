import type {
  AcquisitionErrorCode,
  ApiErrorResponse,
  AudienceMood,
  EmotionBreakdown,
  EmotionLabel,
  IntensityBreakdown,
  IntensityLevel,
  LabelShare,
  SentimentAnalysis,
  SentimentDataset,
  SentimentStats,
} from '../../shared/types';
import { getBackendOrigin } from './http-analysis-service';
import type {
  SentimentService,
  SentimentServiceRequest,
  SentimentServiceResult,
} from './sentiment-service';

const KNOWN_CODES: ReadonlySet<string> = new Set<AcquisitionErrorCode>([
  'invalid_video_id',
  'video_not_found',
  'storage_unavailable',
  'server_not_configured',
  'network_error',
  'upstream_timeout',
  'acquisition_failed',
]);

const FALLBACK_MESSAGE = "We couldn't retrieve the analysis. Try again.";
const TIMEOUT_MESSAGE = 'The request timed out. Please try again.';
const OFFLINE_MESSAGE =
  'Backend is unreachable. Start the local backend (see README) and try again.';

const SENTIMENT_STAT_KEYS: ReadonlyArray<keyof SentimentStats> = [
  'totalComments',
  'analyzed',
  'skipped',
  'positive',
  'neutral',
  'negative',
  'positivePercent',
  'neutralPercent',
  'negativePercent',
];

const SENTIMENT_DATASET_NUMBER_KEYS: ReadonlyArray<keyof SentimentDataset> = [
  'collected',
  'stored',
  'analyzed',
  'skipped',
  'failed',
];

const SENTIMENT_DATASET_BOOL_KEYS: ReadonlyArray<keyof SentimentDataset> = [
  'hasMore',
  'limitReached',
];

const API_STATUSES: ReadonlySet<string> = new Set([
  'NOT_ANALYZED',
  'PROCESSING',
  'PROCESSED',
  'FAILED',
]);

// Sprint 5 vocabulary (docs/13): structural validation of the OPTIONAL
// intelligence blocks - present-but-garbage is rejected like any other
// invalid body (the UI must never render fabricated AI metrics), while a
// pre-Sprint-5 body without them stays valid.
const EMOTION_LABELS: ReadonlyArray<EmotionLabel> = [
  'FEAR',
  'ANGER',
  'ANTICIPATION',
  'TRUST',
  'SURPRISE',
  'SADNESS',
  'DISGUST',
  'JOY',
  'NEUTRAL',
];
const INTENSITY_LEVELS: ReadonlyArray<IntensityLevel> = ['LOW', 'MEDIUM', 'HIGH'];
const AUDIENCE_MOODS: ReadonlyArray<AudienceMood> = [
  'POSITIVE',
  'CALM',
  'EXCITED',
  'MIXED',
  'CONCERNED',
  'NEGATIVE',
];

function isLabelShare(value: unknown): value is LabelShare {
  if (typeof value !== 'object' || value === null) return false;
  const share = value as Partial<LabelShare>;
  return (
    typeof share.count === 'number' &&
    Number.isFinite(share.count) &&
    typeof share.percent === 'number' &&
    Number.isFinite(share.percent)
  );
}

function isDistribution(value: unknown, labels: ReadonlyArray<string>): boolean {
  if (typeof value !== 'object' || value === null) return false;
  const distribution = value as Record<string, unknown>;
  return labels.every((label) => isLabelShare(distribution[label]));
}

/** Sprint 5 optional blocks: valid, complete vocabulary, or absent. */
function isValidIntelligenceBlocks(body: Partial<SentimentAnalysis>): boolean {
  const { emotion, intensity, confidence, audienceMood } = body;
  if (emotion !== undefined) {
    const block = emotion as Partial<EmotionBreakdown> | undefined;
    if (typeof block !== 'object' || block === null) return false;
    if (
      !(
        block.dominant === null ||
        EMOTION_LABELS.includes(block.dominant as EmotionLabel)
      ) ||
      typeof block.dominantPercent !== 'number' ||
      !Number.isFinite(block.dominantPercent) ||
      !isDistribution(block.distribution, EMOTION_LABELS)
    ) {
      return false;
    }
  }
  if (intensity !== undefined) {
    const block = intensity as Partial<IntensityBreakdown> | undefined;
    if (typeof block !== 'object' || block === null) return false;
    if (
      !(
        block.overall === null ||
        INTENSITY_LEVELS.includes(block.overall as IntensityLevel)
      ) ||
      !isDistribution(block.distribution, INTENSITY_LEVELS)
    ) {
      return false;
    }
  }
  if (confidence !== undefined) {
    const average = (confidence as { average?: unknown }).average;
    if (average !== null && typeof average !== 'number') return false;
    if (typeof average === 'number' && !Number.isFinite(average)) return false;
  }
  if (
    audienceMood !== undefined &&
    audienceMood !== null &&
    !AUDIENCE_MOODS.includes(audienceMood as AudienceMood)
  ) {
    return false;
  }
  return true;
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    (error as { name?: unknown }).name === 'AbortError'
  );
}

/** Structural validation: never trust a response body blindly. */
function isSentimentAnalysis(body: unknown): body is SentimentAnalysis {
  if (typeof body !== 'object' || body === null) return false;
  const candidate = body as Partial<SentimentAnalysis>;
  if (typeof candidate.videoId !== 'string') return false;
  if (typeof candidate.status !== 'string' || !API_STATUSES.has(candidate.status)) {
    return false;
  }
  const stats = candidate.stats as Partial<SentimentStats> | undefined;
  if (typeof stats !== 'object' || stats === null) return false;
  if (!SENTIMENT_STAT_KEYS.every((key) => typeof stats[key] === 'number')) {
    return false;
  }
  // Sprint 4.1 dataset metrics are REQUIRED: the overlay renders collected /
  // analyzed / skipped from them, so a body without them is garbage.
  const dataset = candidate.dataset as Partial<SentimentDataset> | undefined;
  if (typeof dataset !== 'object' || dataset === null) return false;
  if (
    !SENTIMENT_DATASET_NUMBER_KEYS.every(
      (key) => typeof dataset[key] === 'number',
    ) ||
    !SENTIMENT_DATASET_BOOL_KEYS.every((key) => typeof dataset[key] === 'boolean')
  ) {
    return false;
  }
  const dominant = candidate.dominantSentiment;
  return (
    dominant === null ||
    dominant === 'POSITIVE' ||
    dominant === 'NEUTRAL' ||
    dominant === 'NEGATIVE'
  ) && isValidIntelligenceBlocks(candidate);
}

/**
 * Real sentiment client: GET {origin}/api/videos/{videoId}/sentiment with a
 * timeout. The backend runs the model, applies the language policy, and
 * aggregates; this class only transports and categorizes - no analysis, no
 * API keys, no raw comment text through the extension.
 */
export class HttpSentimentService implements SentimentService {
  private readonly origin: string;
  private readonly timeoutMs: number;

  constructor(origin: string = getBackendOrigin(), timeoutMs: number = 30000) {
    this.origin = origin;
    this.timeoutMs = timeoutMs;
  }

  async getSentiment(
    request: SentimentServiceRequest,
  ): Promise<SentimentServiceResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(
        `${this.origin}/api/videos/${encodeURIComponent(request.videoId)}/sentiment`,
        {
          method: 'GET',
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        },
      );

      if (!response.ok) {
        let code: AcquisitionErrorCode = 'acquisition_failed';
        let message = FALLBACK_MESSAGE;
        try {
          const body = (await response.json()) as Partial<ApiErrorResponse>;
          if (typeof body.error?.code === 'string' && KNOWN_CODES.has(body.error.code)) {
            code = body.error.code as AcquisitionErrorCode;
          }
          if (typeof body.error?.message === 'string' && body.error.message) {
            message = body.error.message;
          }
        } catch {
          // Non-JSON error body: keep the generic fallback (no raw leakage).
        }
        return { kind: 'error', code, message };
      }

      const body: unknown = await response.json();
      if (!isSentimentAnalysis(body)) {
        return {
          kind: 'error',
          code: 'upstream_data_invalid',
          message: FALLBACK_MESSAGE,
        };
      }
      return { kind: 'success', data: body };
    } catch (error) {
      if (isAbortError(error)) {
        return { kind: 'error', code: 'upstream_timeout', message: TIMEOUT_MESSAGE };
      }
      return { kind: 'error', code: 'network_error', message: OFFLINE_MESSAGE };
    } finally {
      clearTimeout(timer);
    }
  }
}
