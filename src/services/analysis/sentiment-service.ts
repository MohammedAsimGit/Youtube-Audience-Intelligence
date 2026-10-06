import type {
  AcquisitionErrorCode,
  PageKind,
  SentimentAnalysis,
} from '../../shared/types';

export interface SentimentServiceRequest {
  videoId: string;
  pageKind: PageKind;
}

/**
 * Sprint 4 result contract for GET /api/videos/{videoId}/sentiment:
 * - `success`     → backend aggregates (the backend owns model execution;
 *                   this layer never classifies anything itself)
 * - `error`       → categorized failure with friendly copy (no stack traces)
 * - `unavailable` → honest "service not connected" (offline/dev stub only)
 */
export type SentimentServiceResult =
  | { kind: 'success'; data: SentimentAnalysis }
  | { kind: 'error'; code: AcquisitionErrorCode; message: string }
  | { kind: 'unavailable'; reason: string };

export interface SentimentService {
  getSentiment(
    request: SentimentServiceRequest,
  ): Promise<SentimentServiceResult>;
}

/**
 * Offline/dev stub: no network I/O, no fabricated results - the UI falls
 * back to its honest "READY TO ANALYZE" state. Production wiring uses
 * {@link HttpSentimentService}; no credentials ever live in the extension.
 */
export class UnconfiguredSentimentService implements SentimentService {
  private readonly delayMs: number;

  constructor(delayMs = 0) {
    this.delayMs = delayMs;
  }

  getSentiment(
    _request: SentimentServiceRequest,
  ): Promise<SentimentServiceResult> {
    return new Promise((resolve) => {
      setTimeout(
        () => resolve({ kind: 'unavailable', reason: 'service_not_configured' }),
        this.delayMs,
      );
    });
  }
}
