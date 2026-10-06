import type {
  AcquisitionErrorCode,
  ApiErrorResponse,
  RealtimeActivityLevel,
  RealtimeStatus,
  RealtimeTrendState,
} from '../../shared/types';
import { getBackendOrigin } from './http-analysis-service';
import type {
  RealtimeService,
  RealtimeServiceRequest,
  RealtimeServiceResult,
} from './realtime-service';

const KNOWN_CODES: ReadonlySet<string> = new Set<AcquisitionErrorCode>([
  'invalid_video_id',
  'video_not_found',
  'storage_unavailable',
  'server_not_configured',
  'network_error',
  'upstream_timeout',
  'acquisition_failed',
]);

const FALLBACK_MESSAGE = "We couldn't reach the realtime monitor. Retrying shortly.";
const TIMEOUT_MESSAGE = 'The realtime check timed out. Retrying shortly.';
const OFFLINE_MESSAGE =
  'Backend is unreachable. Start the local backend (see README) and try again.';

const TREND_STATES: ReadonlySet<string> = new Set<RealtimeTrendState>([
  'RISING',
  'FALLING',
  'STABLE',
]);
const ACTIVITY_LEVELS: ReadonlySet<string> = new Set<RealtimeActivityLevel>([
  'LOW',
  'MODERATE',
  'HIGH',
]);

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

/**
 * Structural validation: never trust a response body blindly - a malformed
 * monitor payload must never drive the strip or a refetch (§15). The
 * `version` marker is the only field the refresh logic depends on beyond
 * the counts, so it must be a string.
 */
function isRealtimeStatus(body: unknown): body is RealtimeStatus {
  if (typeof body !== 'object' || body === null) return false;
  const candidate = body as Record<string, unknown>;
  if (typeof candidate.videoId !== 'string') return false;
  if (typeof candidate.enabled !== 'boolean') return false;
  if (typeof candidate.monitoring !== 'boolean') return false;
  if (!isFiniteNumber(candidate.pollIntervalSeconds)) return false;
  if (!isNullableString(candidate.lastCheckedAt)) return false;
  if (!isNullableString(candidate.lastUpdatedAt)) return false;
  if (!isFiniteNumber(candidate.newComments)) return false;
  if (!isFiniteNumber(candidate.totalComments)) return false;
  if (!isFiniteNumber(candidate.analyzed)) return false;
  if (!isFiniteNumber(candidate.pending)) return false;
  if (!isFiniteNumber(candidate.skipped)) return false;
  if (!isFiniteNumber(candidate.failed)) return false;
  if (typeof candidate.version !== 'string') return false;
  if (!isNullableString(candidate.dominantEmotion)) return false;

  const sentiment = candidate.sentiment as Record<string, unknown> | undefined;
  if (
    typeof sentiment !== 'object' ||
    sentiment === null ||
    !isFiniteNumber(sentiment.positive) ||
    !isFiniteNumber(sentiment.neutral) ||
    !isFiniteNumber(sentiment.negative)
  ) {
    return false;
  }
  const trend = candidate.trend as Record<string, unknown> | undefined;
  if (
    typeof trend !== 'object' ||
    trend === null ||
    typeof trend.state !== 'string' ||
    !TREND_STATES.has(trend.state) ||
    !isFiniteNumber(trend.changePp) ||
    !isFiniteNumber(trend.positivePp) ||
    !isFiniteNumber(trend.neutralPp) ||
    !isFiniteNumber(trend.negativePp)
  ) {
    return false;
  }
  const activity = candidate.activity as Record<string, unknown> | undefined;
  return (
    typeof activity === 'object' &&
    activity !== null &&
    typeof activity.level === 'string' &&
    ACTIVITY_LEVELS.has(activity.level) &&
    isFiniteNumber(activity.newRecent) &&
    isFiniteNumber(activity.windowMinutes) &&
    isFiniteNumber(activity.ratePerMinute)
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
 * Real monitor client: GET {origin}/api/videos/{videoId}/realtime with a
 * short timeout (cheap local endpoint, polled on an interval). Polling it
 * keeps the backend monitor alive for the active video; responses only
 * transport backend-computed numbers - this layer never derives trends,
 * activity or percentages itself (§4: no fabricated movement).
 */
export class HttpRealtimeService implements RealtimeService {
  private readonly origin: string;
  private readonly timeoutMs: number;

  constructor(origin: string = getBackendOrigin(), timeoutMs = 10000) {
    this.origin = origin;
    this.timeoutMs = timeoutMs;
  }

  async getRealtime(
    request: RealtimeServiceRequest,
  ): Promise<RealtimeServiceResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(
        `${this.origin}/api/videos/${encodeURIComponent(request.videoId)}/realtime`,
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
      if (!isRealtimeStatus(body)) {
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
