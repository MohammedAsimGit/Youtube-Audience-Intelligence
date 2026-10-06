import type {
  AcquisitionErrorCode,
  ApiErrorResponse,
  EmotionBreakdown,
  EmotionLabel,
  IntensityBreakdown,
  IntensityLevel,
  LabelShare,
  TopicAnalysis,
  TopicCategory,
  TopicItem,
} from '../../shared/types';
import { getBackendOrigin } from './http-analysis-service';
import type {
  TopicsService,
  TopicsServiceRequest,
  TopicsServiceResult,
} from './topics-service';

const KNOWN_CODES: ReadonlySet<string> = new Set<AcquisitionErrorCode>([
  'invalid_video_id',
  'video_not_found',
  'storage_unavailable',
  'server_not_configured',
  'network_error',
  'upstream_timeout',
  'acquisition_failed',
]);

const FALLBACK_MESSAGE = "We couldn't retrieve audience topics. Try again.";
const TIMEOUT_MESSAGE = 'The request timed out. Please try again.';
const OFFLINE_MESSAGE =
  'Backend is unreachable. Start the local backend (see README) and try again.';

const TOPIC_STATUSES: ReadonlySet<string> = new Set(['READY', 'INSUFFICIENT_DATA']);

const TOPIC_CATEGORIES: ReadonlySet<string> = new Set<TopicCategory>([
  'MOST_DISCUSSED',
  'APPRECIATED',
  'PAIN_POINT',
  'MIXED',
]);

const SENTIMENT_LABELS: ReadonlySet<string> = new Set([
  'POSITIVE',
  'NEUTRAL',
  'NEGATIVE',
]);

const EMOTION_LABELS: ReadonlyArray<EmotionLabel> = [
  'FEAR', 'ANGER', 'ANTICIPATION', 'TRUST', 'SURPRISE', 'SADNESS', 'DISGUST', 'JOY', 'NEUTRAL',
];
const INTENSITY_LEVELS: ReadonlyArray<IntensityLevel> = ['LOW', 'MEDIUM', 'HIGH'];

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

function isShareMap(value: unknown, labels: ReadonlyArray<string>): boolean {
  if (typeof value !== 'object' || value === null) return false;
  const map = value as Record<string, unknown>;
  return labels.every((label) => isLabelShare(map[label]));
}

/**
 * Structural validation of one topic item: present-but-garbage is
 * rejected like any other invalid body (the UI must never render
 * fabricated intelligence), while every documented field must be there.
 */
function isTopicItem(value: unknown): value is TopicItem {
  if (typeof value !== 'object' || value === null) return false;
  const item = value as Partial<TopicItem>;
  if (typeof item.topicId !== 'string' || typeof item.label !== 'string') return false;
  if (typeof item.mentions !== 'number' || !Number.isFinite(item.mentions)) return false;
  if (typeof item.sharePercent !== 'number' || !Number.isFinite(item.sharePercent)) {
    return false;
  }
  if (typeof item.confidence !== 'number' || !Number.isFinite(item.confidence)) {
    return false;
  }
  if (typeof item.evidence !== 'string') return false;
  if (typeof item.category !== 'string' || !TOPIC_CATEGORIES.has(item.category)) {
    return false;
  }
  if (!Array.isArray(item.keyPhrases) || !item.keyPhrases.every((p) => typeof p === 'string')) {
    return false;
  }
  if (!isShareMap(item.sentiment, ['POSITIVE', 'NEUTRAL', 'NEGATIVE'])) return false;
  const dominant = item.dominantSentiment;
  if (dominant !== null && dominant !== undefined && !SENTIMENT_LABELS.has(dominant)) {
    return false;
  }
  if (item.emotion !== undefined) {
    const emotion = item.emotion as Partial<EmotionBreakdown> | undefined;
    if (typeof emotion !== 'object' || emotion === null) return false;
    if (
      !(
        emotion.dominant === null ||
        emotion.dominant === undefined ||
        EMOTION_LABELS.includes(emotion.dominant as EmotionLabel)
      ) ||
      typeof emotion.dominantPercent !== 'number' ||
      !isShareMap(emotion.distribution, EMOTION_LABELS)
    ) {
      return false;
    }
  }
  if (item.intensity !== undefined) {
    const intensity = item.intensity as Partial<IntensityBreakdown> | undefined;
    if (typeof intensity !== 'object' || intensity === null) return false;
    if (
      !(
        intensity.overall === null ||
        intensity.overall === undefined ||
        INTENSITY_LEVELS.includes(intensity.overall as IntensityLevel)
      ) ||
      !isShareMap(intensity.distribution, INTENSITY_LEVELS)
    ) {
      return false;
    }
  }
  return true;
}

function isTopicList(value: unknown): value is TopicItem[] {
  return Array.isArray(value) && value.every(isTopicItem);
}

/** Structural validation: never trust a response body blindly. */
function isTopicAnalysis(body: unknown): body is TopicAnalysis {
  if (typeof body !== 'object' || body === null) return false;
  const candidate = body as Partial<TopicAnalysis>;
  if (typeof candidate.videoId !== 'string') return false;
  if (typeof candidate.status !== 'string' || !TOPIC_STATUSES.has(candidate.status)) {
    return false;
  }
  if (
    typeof candidate.analyzedComments !== 'number' ||
    !Number.isFinite(candidate.analyzedComments)
  ) {
    return false;
  }
  if (
    candidate.message !== null &&
    candidate.message !== undefined &&
    typeof candidate.message !== 'string'
  ) {
    return false;
  }
  return (
    isTopicList(candidate.topics) &&
    isTopicList(candidate.mostDiscussed) &&
    isTopicList(candidate.mostAppreciated) &&
    isTopicList(candidate.painPoints) &&
    isTopicList(candidate.mixedTopics)
  );
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    (error as { name?: unknown }).name === 'AbortError'
  );
}

/**
 * Real topic client: GET {origin}/api/videos/{videoId}/topics with a
 * timeout. The backend performs data-driven discovery over the analyzed
 * comments; this class only transports and categorizes - no analysis, no
 * raw comment text, no fabricated themes through the extension.
 */
export class HttpTopicsService implements TopicsService {
  private readonly origin: string;
  private readonly timeoutMs: number;

  constructor(origin: string = getBackendOrigin(), timeoutMs = 30000) {
    this.origin = origin;
    this.timeoutMs = timeoutMs;
  }

  async getTopics(
    request: TopicsServiceRequest,
  ): Promise<TopicsServiceResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(
        `${this.origin}/api/videos/${encodeURIComponent(request.videoId)}/topics`,
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
      if (!isTopicAnalysis(body)) {
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
