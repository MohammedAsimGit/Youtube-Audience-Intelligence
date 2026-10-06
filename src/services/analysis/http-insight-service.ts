import type {
  AcquisitionErrorCode,
  ApiErrorResponse,
  InsightAnalysis,
  InsightCard,
  InsightCardCategory,
  InsightEvidenceKind,
} from '../../shared/types';
import { getBackendOrigin } from './http-analysis-service';
import type {
  InsightService,
  InsightServiceRequest,
  InsightServiceResult,
} from './insight-service';

const KNOWN_CODES: ReadonlySet<string> = new Set<AcquisitionErrorCode>([
  'invalid_video_id',
  'video_not_found',
  'storage_unavailable',
  'server_not_configured',
  'network_error',
  'upstream_timeout',
  'acquisition_failed',
]);

const FALLBACK_MESSAGE = "We couldn't retrieve the audience insight. Try again.";
const TIMEOUT_MESSAGE = 'The request timed out. Please try again.';
const OFFLINE_MESSAGE =
  'Backend is unreachable. Start the local backend (see README) and try again.';

const INSIGHT_STATUSES: ReadonlySet<string> = new Set(['READY', 'INSUFFICIENT_DATA']);
const INSIGHT_SOURCES: ReadonlySet<string> = new Set(['deterministic', 'llm', 'fallback']);
const CARD_CATEGORIES: ReadonlySet<string> = new Set<InsightCardCategory>([
  'OVERALL_REACTION',
  'WHAT_WORKED',
  'MAIN_DISCUSSION',
  'PAIN_POINT',
  'EMOTIONAL_SIGNAL',
  'TAKEAWAY',
]);
const EVIDENCE_KINDS: ReadonlySet<string> = new Set<InsightEvidenceKind>([
  'SENTIMENT',
  'EMOTION',
  'INTENSITY',
  'CONFIDENCE',
  'TOPIC',
  'APPRECIATED',
  'PAIN_POINT',
  'MIXED',
  'SAMPLE',
]);

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

function isEvidenceLine(value: unknown): boolean {
  if (typeof value !== 'object' || value === null) return false;
  const line = value as Record<string, unknown>;
  return (
    typeof line.kind === 'string' &&
    EVIDENCE_KINDS.has(line.kind) &&
    typeof line.label === 'string' &&
    typeof line.value === 'string' &&
    isNullableString(line.detail) &&
    isNullableString(line.topicId)
  );
}

function isCard(value: unknown): value is InsightCard {
  if (typeof value !== 'object' || value === null) return false;
  const card = value as Record<string, unknown>;
  return (
    typeof card.category === 'string' &&
    CARD_CATEGORIES.has(card.category) &&
    typeof card.title === 'string' &&
    typeof card.body === 'string' &&
    Array.isArray(card.evidence) &&
    card.evidence.every(isEvidenceLine)
  );
}

/**
 * Structural validation: never trust a response body blindly - malformed
 * AI output must never render (§15). Present-but-garbage is rejected like
 * any other invalid body.
 */
function isInsightAnalysis(body: unknown): body is InsightAnalysis {
  if (typeof body !== 'object' || body === null) return false;
  const candidate = body as Record<string, unknown>;
  if (typeof candidate.videoId !== 'string') return false;
  if (typeof candidate.status !== 'string' || !INSIGHT_STATUSES.has(candidate.status)) {
    return false;
  }
  if (!isNullableString(candidate.message)) return false;
  if (typeof candidate.headline !== 'string') return false;
  if (typeof candidate.summary !== 'string') return false;
  if (!Array.isArray(candidate.cards) || !candidate.cards.every(isCard)) return false;
  if (typeof candidate.source !== 'string' || !INSIGHT_SOURCES.has(candidate.source)) {
    return false;
  }
  if (typeof candidate.evidenceVersion !== 'string') return false;
  if (!isNullableString(candidate.generatedAt)) return false;
  if (!isFiniteNumber(candidate.generationMs)) return false;

  const sample = candidate.sample as Record<string, unknown> | undefined;
  if (
    typeof sample !== 'object' ||
    sample === null ||
    !isFiniteNumber(sample.collected) ||
    !isFiniteNumber(sample.analyzed) ||
    !isFiniteNumber(sample.skipped)
  ) {
    return false;
  }
  const provider = candidate.provider as Record<string, unknown> | undefined;
  return (
    typeof provider === 'object' &&
    provider !== null &&
    typeof provider.name === 'string' &&
    isNullableString(provider.model)
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
 * Real insight client: GET {origin}/api/videos/{videoId}/insight with a
 * timeout. The backend builds the evidence and phrases it through its
 * provider layer; this class only transports and categorizes - no
 * summary text is ever written client-side (§5).
 */
export class HttpInsightService implements InsightService {
  private readonly origin: string;
  private readonly timeoutMs: number;

  constructor(origin: string = getBackendOrigin(), timeoutMs = 30000) {
    this.origin = origin;
    this.timeoutMs = timeoutMs;
  }

  async getInsight(
    request: InsightServiceRequest,
  ): Promise<InsightServiceResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(
        `${this.origin}/api/videos/${encodeURIComponent(request.videoId)}/insight`,
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
      if (!isInsightAnalysis(body)) {
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
