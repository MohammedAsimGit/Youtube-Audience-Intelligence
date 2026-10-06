import type { PageKind, TopicAnalysis } from '../../shared/types';

export interface TopicsServiceRequest {
  videoId: string;
  pageKind: PageKind;
}

/**
 * Sprint 6 result contract for GET /api/videos/{videoId}/topics:
 * - `success`     → backend topic discovery (the backend owns the whole
 *                   computation; this layer never derives themes itself)
 * - `error`       → categorized failure with friendly copy (no stack
 *                   traces). The overlay keeps sentiment intact and shows
 *                   "Audience topics unavailable" (§39).
 * - `unavailable` → honest "service not connected" (offline/dev stub only)
 */
export type TopicsServiceResult =
  | { kind: 'success'; data: TopicAnalysis }
  | { kind: 'error'; code: import('../../shared/types').AcquisitionErrorCode; message: string }
  | { kind: 'unavailable'; reason: string };

export interface TopicsService {
  getTopics(request: TopicsServiceRequest): Promise<TopicsServiceResult>;
}

/**
 * Offline/dev stub: no network I/O, no fabricated topics - the overlay
 * simply renders no discussion section (honest absent state).
 */
export class UnconfiguredTopicsService implements TopicsService {
  getTopics(_request: TopicsServiceRequest): Promise<TopicsServiceResult> {
    return Promise.resolve({ kind: 'unavailable', reason: 'service_not_configured' });
  }
}
