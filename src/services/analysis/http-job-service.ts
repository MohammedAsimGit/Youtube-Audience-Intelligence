import type {
  AcquisitionErrorCode,
  AnalysisJob,
  AnalysisJobCreated,
  AnalysisJobStatusValue,
  ApiErrorResponse,
} from '../../shared/types';
import {
  DEFAULT_POLL_INTERVAL_MS,
  TERMINAL_JOB_STATUSES,
} from './analysis-job-service';
import type {
  AnalysisJobService,
  JobStartRequest,
  JobStartResult,
  JobWaitRequest,
  JobWaitResult,
} from './analysis-job-service';
import { getBackendOrigin } from './http-analysis-service';

const FALLBACK_MESSAGE = "We couldn't start the analysis. Please try again.";
const STATUS_FALLBACK_MESSAGE =
  'Analysis status is unavailable. Please try again.';
const OFFLINE_MESSAGE =
  'Backend is unreachable. Start the local backend (see README) and try again.';

const KNOWN_CODES: ReadonlySet<string> = new Set<AcquisitionErrorCode>([
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

const JOB_STATUSES: ReadonlySet<string> = new Set<AnalysisJobStatusValue>([
  'QUEUED',
  'ACQUIRING',
  'ANALYZING',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'STALE',
]);

const JOB_PHASES: ReadonlySet<string> = new Set([
  'NONE',
  'ACQUISITION',
  'SENTIMENT',
  'TOPIC',
  'INSIGHT',
  'COMPLETE',
]);

const JOB_NUMBER_KEYS: ReadonlyArray<keyof AnalysisJob> = [
  'collected',
  'stored',
  'analyzable',
  'analyzed',
  'skipped',
  'failed',
  'pending',
];

/** Structural validation: never trust a response body blindly (§31). */
function isAnalysisJob(body: unknown): body is AnalysisJob {
  if (typeof body !== 'object' || body === null) return false;
  const job = body as Partial<AnalysisJob>;
  if (typeof job.jobId !== 'string' || typeof job.videoId !== 'string') return false;
  if (typeof job.status !== 'string' || !JOB_STATUSES.has(job.status)) return false;
  if (typeof job.phase !== 'string' || !JOB_PHASES.has(job.phase)) return false;
  if (!JOB_NUMBER_KEYS.every((key) => typeof job[key] === 'number')) return false;
  if (typeof job.hasMore !== 'boolean') return false;
  return true;
}

function isJobCreated(body: unknown): body is AnalysisJobCreated {
  if (typeof body !== 'object' || body === null) return false;
  const job = body as Partial<AnalysisJobCreated>;
  return (
    typeof job.jobId === 'string' &&
    typeof job.videoId === 'string' &&
    typeof job.status === 'string' &&
    JOB_STATUSES.has(job.status)
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

/** Abort-aware sleep: resolves early when the signal fires (§26/§27). */
function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

async function readErrorEnvelope(
  response: Response,
  fallback: string,
): Promise<{ code: AcquisitionErrorCode; message: string }> {
  let code: AcquisitionErrorCode = 'acquisition_failed';
  let message = fallback;
  try {
    const body = (await response.json()) as Partial<ApiErrorResponse>;
    if (typeof body.error?.code === 'string' && KNOWN_CODES.has(body.error.code)) {
      code = body.error.code as AcquisitionErrorCode;
    }
    if (typeof body.error?.message === 'string' && body.error.message) {
      message = body.error.message;
    }
  } catch {
    // Non-JSON error body: keep the categorized fallback (no raw leakage).
  }
  return { code, message };
}

/**
 * Real background-job client (Sprint 4.3):
 * - `start`: POST, short-lived request, 202 Accepted (§9);
 * - `waitForTerminal`: poll -> wait -> poll at a fixed interval, stopping
 *   on any terminal status, on abort (video switch), or after a bounded
 *   run of consecutive transport failures (§26 - never `while(true)`).
 */
export class HttpJobService implements AnalysisJobService {
  private readonly origin: string;
  private readonly timeoutMs: number;
  private readonly pollIntervalMs: number;
  private readonly maxConsecutiveErrors: number;

  constructor(
    origin: string = getBackendOrigin(),
    timeoutMs: number = 10000,
    pollIntervalMs: number = DEFAULT_POLL_INTERVAL_MS,
    maxConsecutiveErrors: number = 3,
  ) {
    this.origin = origin;
    this.timeoutMs = timeoutMs;
    this.pollIntervalMs = pollIntervalMs;
    this.maxConsecutiveErrors = maxConsecutiveErrors;
  }

  async start(request: JobStartRequest): Promise<JobStartResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(
        `${this.origin}/api/videos/${encodeURIComponent(request.videoId)}/analysis`,
        {
          method: 'POST',
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        },
      );
      if (!response.ok) {
        const { code, message } = await readErrorEnvelope(response, FALLBACK_MESSAGE);
        return { kind: 'error', code, message };
      }
      const body: unknown = await response.json();
      if (!isJobCreated(body)) {
        return {
          kind: 'error',
          code: 'upstream_data_invalid',
          message: FALLBACK_MESSAGE,
        };
      }
      return { kind: 'started', job: body };
    } catch (error) {
      if (isAbortError(error)) {
        return { kind: 'error', code: 'upstream_timeout', message: FALLBACK_MESSAGE };
      }
      return { kind: 'error', code: 'network_error', message: OFFLINE_MESSAGE };
    } finally {
      clearTimeout(timer);
    }
  }

  /** One short-lived status request (§10); `none` = no job exists yet. */
  async getStatus(
    videoId: string,
    signal?: AbortSignal,
  ): Promise<
    | { kind: 'success'; job: AnalysisJob }
    | { kind: 'none' }
    | { kind: 'error'; code: AcquisitionErrorCode; message: string }
  > {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const onAbort = (): void => controller.abort();
    signal?.addEventListener('abort', onAbort, { once: true });
    try {
      const response = await fetch(
        `${this.origin}/api/videos/${encodeURIComponent(videoId)}/analysis/status`,
        {
          method: 'GET',
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        },
      );
      if (response.status === 404) {
        return { kind: 'none' };
      }
      if (!response.ok) {
        const { code, message } = await readErrorEnvelope(
          response,
          STATUS_FALLBACK_MESSAGE,
        );
        return { kind: 'error', code, message };
      }
      const body: unknown = await response.json();
      if (!isAnalysisJob(body)) {
        return {
          kind: 'error',
          code: 'upstream_data_invalid',
          message: STATUS_FALLBACK_MESSAGE,
        };
      }
      return { kind: 'success', job: body };
    } catch (error) {
      if (isAbortError(error)) {
        return { kind: 'error', code: 'upstream_timeout', message: STATUS_FALLBACK_MESSAGE };
      }
      return { kind: 'error', code: 'network_error', message: OFFLINE_MESSAGE };
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
    }
  }

  async waitForTerminal(request: JobWaitRequest): Promise<JobWaitResult> {
    const { videoId, signal, onJob } = request;
    let consecutiveErrors = 0;
    for (;;) {
      if (signal?.aborted) return { kind: 'aborted' };
      const status = await this.getStatus(videoId, signal);
      if (signal?.aborted) return { kind: 'aborted' };

      if (status.kind === 'error') {
        consecutiveErrors += 1;
        if (consecutiveErrors >= this.maxConsecutiveErrors) {
          return { kind: 'error', code: status.code, message: status.message };
        }
      } else if (status.kind === 'none') {
        // The job row vanished (should not happen mid-run): count it as a
        // transient inconsistency, bounded like a transport error.
        consecutiveErrors += 1;
        if (consecutiveErrors >= this.maxConsecutiveErrors) {
          return {
            kind: 'error',
            code: 'acquisition_failed',
            message: STATUS_FALLBACK_MESSAGE,
          };
        }
      } else {
        consecutiveErrors = 0;
        onJob?.(status.job);
        if (TERMINAL_JOB_STATUSES.has(status.job.status)) {
          return status.job.status === 'COMPLETED'
            ? { kind: 'completed', job: status.job }
            : { kind: 'failed', job: status.job };
        }
      }
      await delay(this.pollIntervalMs, signal);
    }
  }
}
