import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AnalysisJob, AnalysisJobCreated } from '../../shared/types';
import { DEFAULT_POLL_INTERVAL_MS } from './analysis-job-service';
import { DEFAULT_BACKEND_ORIGIN } from './http-analysis-service';
import { HttpJobService } from './http-job-service';

const CREATED: AnalysisJobCreated = {
  jobId: 'job-1',
  videoId: 'dQw4w9WgXcQ',
  status: 'QUEUED',
};

function jobFixture(overrides?: Partial<AnalysisJob>): AnalysisJob {
  return {
    jobId: 'job-1',
    videoId: 'dQw4w9WgXcQ',
    status: 'ACQUIRING',
    phase: 'ACQUISITION',
    collected: 0,
    stored: 0,
    analyzable: 0,
    analyzed: 0,
    skipped: 0,
    failed: 0,
    pending: 0,
    hasMore: false,
    errorCode: null,
    errorMessage: null,
    createdAt: '2026-09-27T10:00:00+00:00',
    updatedAt: '2026-09-27T10:00:00+00:00',
    finishedAt: null,
    ...overrides,
  };
}

const REQUEST = { videoId: 'dQw4w9WgXcQ', pageKind: 'watch' as const };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('HttpJobService.start', () => {
  it('POSTs the analysis endpoint and returns the accepted job', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => CREATED,
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await new HttpJobService().start(REQUEST);

    expect(result).toEqual({ kind: 'started', job: CREATED });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${DEFAULT_BACKEND_ORIGIN}/api/videos/dQw4w9WgXcQ/analysis`);
    expect(init.method).toBe('POST');
    // No credentials of any kind are attached (§37).
    expect(JSON.stringify(init.headers ?? {})).not.toMatch(/key|auth|bearer/i);
  });

  it('maps the backend error envelope to categorized copy', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({
          error: { code: 'server_not_configured', message: 'Not configured.' },
        }),
      }),
    );

    const result = await new HttpJobService().start(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'server_not_configured',
      message: 'Not configured.',
    });
  });

  it('maps transport rejection to network_error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new TypeError('Failed to fetch')),
    );

    const result = await new HttpJobService().start(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('network_error');
      expect(result.message).toMatch(/backend is unreachable/i);
    }
  });

  it('rejects a malformed 202 body instead of trusting it', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 202,
        json: async () => ({ unexpected: 'shape' }),
      }),
    );

    const result = await new HttpJobService().start(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'upstream_data_invalid',
      message: expect.stringContaining("couldn't start") as unknown as string,
    });
  });
});

describe('HttpJobService status + polling', () => {
  function jsonResponse(status: number, body: unknown) {
    return { ok: status < 400, status, json: async () => body };
  }

  it('returns none for 404 (no job ever ran)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(404, { error: { code: 'video_not_found' } })),
    );

    const result = await new HttpJobService().getStatus('dQw4w9WgXcQ');
    expect(result).toEqual({ kind: 'none' });
  });

  it('validates the status payload structurally', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(200, { jobId: 42, status: 'BOGUS' })),
    );

    const result = await new HttpJobService().getStatus('dQw4w9WgXcQ');
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('upstream_data_invalid');
    }
  });

  it('polls until a terminal COMPLETED and reports every snapshot', async () => {
    const snapshots = [
      jobFixture({ status: 'ACQUIRING', phase: 'ACQUISITION', collected: 500 }),
      jobFixture({ status: 'ANALYZING', phase: 'SENTIMENT', analyzed: 400, pending: 100 }),
      jobFixture({
        status: 'COMPLETED',
        phase: 'COMPLETE',
        collected: 500,
        stored: 500,
        analyzed: 500,
        pending: 0,
      }),
    ];
    let call = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async () =>
        jsonResponse(200, snapshots[Math.min(call++, snapshots.length - 1)]),
      ),
    );

    const seen: AnalysisJob[] = [];
    const result = await new HttpJobService(undefined, 1000, 1).waitForTerminal({
      ...REQUEST,
      onJob: (job) => seen.push(job),
    });

    expect(result.kind).toBe('completed');
    if (result.kind === 'completed') {
      expect(result.job.status).toBe('COMPLETED');
    }
    // §7: every poll is reported so the overlay can render real progress.
    expect(seen.map((job) => job.status)).toEqual([
      'ACQUIRING',
      'ANALYZING',
      'COMPLETED',
    ]);
  });

  it('returns failed for FAILED / CANCELLED / STALE terminal states', async () => {
    for (const status of ['FAILED', 'CANCELLED', 'STALE'] as const) {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(jsonResponse(200, jobFixture({ status }))),
      );
      const result = await new HttpJobService(undefined, 1000, 1).waitForTerminal(REQUEST);
      expect(result.kind).toBe('failed');
      if (result.kind === 'failed') {
        expect(result.job.status).toBe(status);
      }
    }
  });

  it('stops polling as soon as the signal aborts (video switch)', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(200, jobFixture({ status: 'ACQUIRING' })));
    vi.stubGlobal('fetch', fetchMock);

    const controller = new AbortController();
    const pending = new HttpJobService(undefined, 1000, 500).waitForTerminal({
      ...REQUEST,
      signal: controller.signal,
    });
    // Abort while the poller sits in its inter-poll wait.
    setTimeout(() => controller.abort(), 20);
    const result = await pending;

    expect(result).toEqual({ kind: 'aborted' });
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(2);
  });

  it('returns aborted immediately for a pre-aborted signal without fetching', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    const controller = new AbortController();
    controller.abort();
    const result = await new HttpJobService().waitForTerminal({
      ...REQUEST,
      signal: controller.signal,
    });

    expect(result).toEqual({ kind: 'aborted' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('bounds consecutive 404s instead of polling forever (§26)', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(404, { error: { code: 'video_not_found' } }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await new HttpJobService(undefined, 1000, 1, 3).waitForTerminal(REQUEST);

    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.message).toMatch(/status is unavailable/i);
    }
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('uses the documented default polling interval', () => {
    expect(DEFAULT_POLL_INTERVAL_MS).toBeGreaterThanOrEqual(250);
  });
});
