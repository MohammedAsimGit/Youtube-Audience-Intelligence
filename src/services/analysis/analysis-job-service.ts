import type {
  AcquisitionErrorCode,
  AnalysisJob,
  AnalysisJobCreated,
  AnalysisJobStatusValue,
  PageKind,
} from '../../shared/types';

/**
 * Sprint 4.3: the seam between the overlay and the backend's background
 * job API (docs/13-backend-api-contract.md).
 *
 *   start()          -> POST /api/videos/{id}/analysis (202, returns fast)
 *   waitForTerminal() -> controlled polling of
 *                        GET /api/videos/{id}/analysis/status (§26)
 *
 * The overlay NEVER waits for the pipeline in one request: it starts a
 * job, shows real progress while polling, and reacts to a terminal state.
 */
export interface JobStartRequest {
  videoId: string;
  pageKind: PageKind;
}

export type JobStartResult =
  | { kind: 'started'; job: AnalysisJobCreated }
  | { kind: 'error'; code: AcquisitionErrorCode; message: string };

export interface JobWaitRequest {
  videoId: string;
  pageKind: PageKind;
  /** Aborts polling (video switch / component teardown). */
  signal?: AbortSignal;
  /** Called for EVERY polled snapshot - including the terminal one. */
  onJob?: (job: AnalysisJob) => void;
}

export type JobWaitResult =
  | { kind: 'completed'; job: AnalysisJob }
  | { kind: 'failed'; job: AnalysisJob }
  | { kind: 'aborted' }
  | { kind: 'error'; code: AcquisitionErrorCode; message: string };

export interface AnalysisJobService {
  start(request: JobStartRequest): Promise<JobStartResult>;
  waitForTerminal(request: JobWaitRequest): Promise<JobWaitResult>;
}

/** Polling stops here (§26). CANCELLED/STALE are terminal too. */
export const TERMINAL_JOB_STATUSES: ReadonlySet<AnalysisJobStatusValue> = new Set([
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'STALE',
]);

/** Job error codes the backend can report (mirrors the server taxonomy). */
const JOB_ERROR_CODES: ReadonlySet<string> = new Set<AcquisitionErrorCode>([
  'invalid_video_id',
  'video_not_found',
  'comments_disabled',
  'quota_exceeded',
  'upstream_timeout',
  'upstream_unavailable',
  'upstream_data_invalid',
  'server_not_configured',
  'storage_unavailable',
  'network_error',
  'job_cancelled',
  'job_interrupted',
  'acquisition_failed',
]);

/**
 * Coerce a backend-reported job error code into the extension's known
 * taxonomy; anything unrecognized degrades to `acquisition_failed` so the
 * UI always has honest, categorized copy (never raw server text as a code).
 */
export function asAcquisitionErrorCode(
  code: string | null | undefined,
): AcquisitionErrorCode {
  return code !== null && code !== undefined && JOB_ERROR_CODES.has(code)
    ? (code as AcquisitionErrorCode)
    : 'acquisition_failed';
}

/** Default cadence for job polling (§26: poll -> wait -> poll, never a loop). */
export const DEFAULT_POLL_INTERVAL_MS = 750;
