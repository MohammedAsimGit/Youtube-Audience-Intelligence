import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RealtimeStatus } from '../../shared/types';
import { DEFAULT_BACKEND_ORIGIN } from './http-analysis-service';
import { HttpRealtimeService } from './http-realtime-service';

const REQUEST = { videoId: 'dQw4w9WgXcQ', pageKind: 'watch' as const };

function statusFixture(overrides?: Partial<RealtimeStatus>): RealtimeStatus {
  return {
    videoId: 'dQw4w9WgXcQ',
    enabled: true,
    monitoring: true,
    pollIntervalSeconds: 30,
    lastCheckedAt: '2026-10-02T10:00:00+00:00',
    lastUpdatedAt: '2026-10-02T09:59:00+00:00',
    newComments: 22,
    totalComments: 2429,
    analyzed: 1211,
    pending: 0,
    skipped: 1218,
    failed: 0,
    sentiment: { positive: 68.3, neutral: 29, negative: 2.7 },
    trend: {
      state: 'RISING',
      changePp: 4.2,
      positivePp: 4.2,
      neutralPp: -3.1,
      negativePp: -1.1,
    },
    activity: { level: 'MODERATE', newRecent: 22, windowMinutes: 1, ratePerMinute: 3.4 },
    dominantEmotion: 'TRUST',
    version: '2429:1211',
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('HttpRealtimeService.getRealtime', () => {
  it('GETs the realtime endpoint and returns the validated status', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => statusFixture(),
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await new HttpRealtimeService().getRealtime(REQUEST);

    expect(result).toEqual({ kind: 'success', data: statusFixture() });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${DEFAULT_BACKEND_ORIGIN}/api/videos/dQw4w9WgXcQ/realtime`);
    expect(init.method).toBe('GET');
    // No credentials of any kind are attached (established §37 rule).
    expect(JSON.stringify(init.headers ?? {})).not.toMatch(/key|auth|bearer/i);
  });

  it('rejects a malformed body instead of rendering garbage (§15)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ ...statusFixture(), trend: { state: 'SKYROCKETING' } }),
      }),
    );

    const result = await new HttpRealtimeService().getRealtime(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'upstream_data_invalid',
      message: expect.any(String) as unknown as string,
    });
  });

  it('rejects a body with a non-numeric count (fabrication guard)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ ...statusFixture(), analyzed: 'many' }),
      }),
    );

    const result = await new HttpRealtimeService().getRealtime(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('upstream_data_invalid');
    }
  });

  it('maps the backend error envelope to categorized copy', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({
          error: { code: 'video_not_found', message: 'No stored dataset.' },
        }),
      }),
    );

    const result = await new HttpRealtimeService().getRealtime(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'video_not_found',
      message: 'No stored dataset.',
    });
  });

  it('categorizes timeouts as upstream_timeout', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(Object.assign(new Error('aborted'), { name: 'AbortError' })),
    );

    const result = await new HttpRealtimeService().getRealtime(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('upstream_timeout');
    }
  });

  it('categorizes transport failures as network_error (poll keeps cadence)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));

    const result = await new HttpRealtimeService().getRealtime(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('network_error');
    }
  });
});
