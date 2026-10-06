import type {
  AcquisitionErrorCode,
  PageKind,
  VideoDataResponse,
} from '../../shared/types';

export interface AnalysisServiceRequest {
  videoId: string;
  pageKind: PageKind;
}

/**
 * Result contract (Sprint 2):
 * - `success`  → real acquired data (metadata + comments), never fabricated
 * - `error`    → categorized failure with friendly copy (no stack traces)
 * - `unavailable` → honest "service not connected" (offline/dev stub only)
 *
 * This interface is the seam where the React UI stops and backend
 * communication begins: Chrome UI → AnalysisService → Backend API → YouTube.
 */
export type AnalysisServiceResult =
  | { kind: 'success'; data: VideoDataResponse }
  | { kind: 'error'; code: AcquisitionErrorCode; message: string }
  | { kind: 'unavailable'; reason: string };

export interface AnalysisService {
  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult>;
}

/**
 * Offline/dev stub. It performs NO network I/O and generates NO results -
 * useful when the backend is not running (e.g., UI development without a
 * server). The production wiring uses {@link HttpAnalysisService}; API
 * credentials never live in the extension.
 */
export class UnconfiguredAnalysisService implements AnalysisService {
  private readonly delayMs: number;

  constructor(delayMs = 700) {
    this.delayMs = delayMs;
  }

  analyze(_request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    return new Promise((resolve) => {
      setTimeout(
        () => resolve({ kind: 'unavailable', reason: 'service_not_configured' }),
        this.delayMs,
      );
    });
  }
}
