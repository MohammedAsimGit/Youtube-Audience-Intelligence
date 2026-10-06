import type {
  AcquisitionErrorCode,
  ApiErrorResponse,
  VideoDataResponse,
} from '../../shared/types';
import type {
  AnalysisService,
  AnalysisServiceRequest,
  AnalysisServiceResult,
} from './analysis-service';

/** Local development backend origin (uvicorn default). */
export const DEFAULT_BACKEND_ORIGIN = 'http://127.0.0.1:8000';

const KNOWN_CODES: ReadonlySet<string> = new Set<AcquisitionErrorCode>([
  'invalid_video_id',
  'video_not_found',
  'comments_disabled',
  'quota_exceeded',
  'upstream_timeout',
  'upstream_unavailable',
  'upstream_data_invalid',
  'server_not_configured',
  'network_error',
  'acquisition_failed',
]);

const FALLBACK_MESSAGE =
  "We couldn't retrieve audience responses for this video. Please try again later.";
const TIMEOUT_MESSAGE = 'The request timed out. Please try again.';
const OFFLINE_MESSAGE =
  'Backend is unreachable. Start the local backend (see README) and try again.';

/**
 * Optional build-time override consumed by a custom build step:
 *   __SENTIMENT_AI_BACKEND__ = "https://api.example.com"
 * Not set in this repository's builds; the default targets localhost.
 * No credentials are ever embedded here or anywhere in the extension.
 */
export function getBackendOrigin(): string {
  const scope = globalThis as typeof globalThis & { __SENTIMENT_AI_BACKEND__?: string };
  return scope.__SENTIMENT_AI_BACKEND__ ?? DEFAULT_BACKEND_ORIGIN;
}

function isAbortError(error: unknown): boolean {
  // Works for both DOMException and Error shapes across jsdom/browser/node.
  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    (error as { name?: unknown }).name === 'AbortError'
  );
}

/**
 * Real acquisition client: GET {origin}/api/videos/{videoId} with a timeout.
 * The backend validates, fetches YouTube, normalizes, and returns the stable
 * contract; this class only transports and categorizes - no YouTube access,
 * no API keys, no analysis.
 */
export class HttpAnalysisService implements AnalysisService {
  private readonly origin: string;
  private readonly timeoutMs: number;

  constructor(origin: string = getBackendOrigin(), timeoutMs = 15000) {
    this.origin = origin;
    this.timeoutMs = timeoutMs;
  }

  async analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(
        `${this.origin}/api/videos/${encodeURIComponent(request.videoId)}`,
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

      const data = (await response.json()) as Partial<VideoDataResponse> | null;
      if (!data || typeof data !== 'object' || !data.video || !data.comments) {
        return {
          kind: 'error',
          code: 'upstream_data_invalid',
          message: FALLBACK_MESSAGE,
        };
      }
      return { kind: 'success', data: data as VideoDataResponse };
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
