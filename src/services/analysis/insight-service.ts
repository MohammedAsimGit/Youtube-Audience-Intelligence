import type { AcquisitionErrorCode, InsightAnalysis, PageKind } from '../../shared/types';

export interface InsightServiceRequest {
  videoId: string;
  pageKind: PageKind;
}

/**
 * Sprint 7 result contract for GET /api/videos/{videoId}/insight:
 * - `success`     → backend-generated insight (evidence phrased by the
 *                   backend's provider layer; this layer never writes
 *                   summary text itself)
 * - `error`       → categorized failure with friendly copy (no stack
 *                   traces). The overlay keeps sentiment/topics intact and
 *                   shows "Audience insight unavailable" with Retry (§30).
 * - `unavailable` → honest "service not connected" (offline/dev stub only)
 */
export type InsightServiceResult =
  | { kind: 'success'; data: InsightAnalysis }
  | { kind: 'error'; code: AcquisitionErrorCode; message: string }
  | { kind: 'unavailable'; reason: string };

export interface InsightService {
  getInsight(request: InsightServiceRequest): Promise<InsightServiceResult>;
}

/**
 * Offline/dev stub: no network I/O, no fabricated insight - the overlay
 * simply renders no insight section (honest absent state).
 */
export class UnconfiguredInsightService implements InsightService {
  getInsight(_request: InsightServiceRequest): Promise<InsightServiceResult> {
    return Promise.resolve({ kind: 'unavailable', reason: 'service_not_configured' });
  }
}
