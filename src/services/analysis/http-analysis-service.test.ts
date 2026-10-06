import { afterEach, describe, expect, it, vi } from 'vitest';
import type { VideoDataResponse } from '../../shared/types';
import {
  DEFAULT_BACKEND_ORIGIN,
  HttpAnalysisService,
} from './http-analysis-service';

const FIXTURE: VideoDataResponse = {
  video: {
    videoId: 'dQw4w9WgXcQ',
    title: 'Real Fixture Title',
    description: null,
    channelId: 'UC123',
    channelTitle: 'Fixture Channel',
    publishedAt: '2024-03-01T10:00:00Z',
    categoryId: '28',
    duration: null,
    statistics: { viewCount: 100, likeCount: 5, commentCount: 2 },
  },
  comments: { items: [], count: 2, hasMore: false, status: 'ok' },
  source: { provider: 'youtube', retrievedAt: '2024-03-04T00:00:00Z', cached: false },
};

const REQUEST = { videoId: 'dQw4w9WgXcQ', pageKind: 'watch' as const };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('HttpAnalysisService', () => {
  it('returns success data and calls the documented endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => FIXTURE,
    });
    vi.stubGlobal('fetch', fetchMock);

    const service = new HttpAnalysisService();
    const result = await service.analyze(REQUEST);

    expect(result.kind).toBe('success');
    if (result.kind === 'success') {
      expect(result.data.video.title).toBe('Real Fixture Title');
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${DEFAULT_BACKEND_ORIGIN}/api/videos/dQw4w9WgXcQ`);
    expect(init.method).toBe('GET');
    // No credentials of any kind are attached.
    expect(JSON.stringify(init.headers ?? {})).not.toMatch(/key|auth|bearer/i);
  });

  it('maps error envelopes to categorized results with backend message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 429,
        json: async () => ({
          error: {
            code: 'quota_exceeded',
            message: 'The YouTube API quota is exhausted for now.',
          },
        }),
      }),
    );

    const result = await new HttpAnalysisService().analyze(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'quota_exceeded',
      message: 'The YouTube API quota is exhausted for now.',
    });
  });

  it('falls back safely on non-JSON error bodies', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        json: async () => {
          throw new SyntaxError('not json');
        },
      }),
    );

    const result = await new HttpAnalysisService().analyze(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('acquisition_failed');
      expect(result.message).toMatch(/couldn't retrieve audience responses/i);
      expect(result.message).not.toMatch(/traceback|httpx|502/i);
    }
  });

  it('rejects unknown error codes from untrusted bodies', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        json: async () => ({ error: { code: '../../evil', message: 'weird' } }),
      }),
    );

    const result = await new HttpAnalysisService().analyze(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'acquisition_failed',
      message: 'weird',
    });
  });

  it('flags malformed success bodies as upstream_data_invalid', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ nope: 1 }) }),
    );

    const result = await new HttpAnalysisService().analyze(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('upstream_data_invalid');
    }
  });

  it('reports network failure as an offline backend (friendly copy)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new TypeError('Failed to fetch')),
    );

    const result = await new HttpAnalysisService().analyze(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('network_error');
      expect(result.message).toMatch(/backend is unreachable/i);
    }
  });

  it('aborts slow requests and reports a timeout', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () => {
              const error = new Error('aborted');
              error.name = 'AbortError';
              reject(error);
            });
          }),
      ),
    );

    const result = await new HttpAnalysisService('http://127.0.0.1:8000', 5).analyze(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'upstream_timeout',
      message: 'The request timed out. Please try again.',
    });
  });

  it('honors the build-time backend origin override', async () => {
    const scope = globalThis as typeof globalThis & { __SENTIMENT_AI_BACKEND__?: string };
    scope.__SENTIMENT_AI_BACKEND__ = 'https://api.example.test';
    try {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => FIXTURE,
      });
      vi.stubGlobal('fetch', fetchMock);
      await new HttpAnalysisService().analyze(REQUEST);
      expect(String(fetchMock.mock.calls[0][0])).toContain('https://api.example.test/');
    } finally {
      delete scope.__SENTIMENT_AI_BACKEND__;
    }
  });
});
