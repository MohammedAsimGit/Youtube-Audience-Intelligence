import type {
  AcquisitionErrorCode,
  PageKind,
  RealtimeStatus,
} from '../../shared/types';

export interface RealtimeServiceRequest {
  videoId: string;
  pageKind: PageKind;
}

/**
 * Sprint 8 result contract for GET /api/videos/{videoId}/realtime:
 * - `success`     → live backend status (counts + monitor snapshot). The
 *                   GET doubles as the monitor's keep-alive touch, so the
 *                   overlay's polling cadence IS what sustains backend
 *                   monitoring while the user stays on the video.
 * - `error`       → categorized failure with friendly copy (no stack
 *                   traces). The overlay keeps its last known status and
 *                   simply retries on the next interval (§15 error recovery).
 * - `unavailable` → honest "service not connected" (offline/dev stub only)
 */
export type RealtimeServiceResult =
  | { kind: 'success'; data: RealtimeStatus }
  | { kind: 'error'; code: AcquisitionErrorCode; message: string }
  | { kind: 'unavailable'; reason: string };

export interface RealtimeService {
  getRealtime(request: RealtimeServiceRequest): Promise<RealtimeServiceResult>;
}

/**
 * Offline/dev stub: no network I/O, no fabricated movement - the overlay
 * renders no live strip (honest absent state).
 */
export class UnconfiguredRealtimeService implements RealtimeService {
  getRealtime(_request: RealtimeServiceRequest): Promise<RealtimeServiceResult> {
    return Promise.resolve({ kind: 'unavailable', reason: 'service_not_configured' });
  }
}
