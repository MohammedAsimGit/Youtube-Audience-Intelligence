import { afterEach, describe, expect, it, vi } from 'vitest';
import type { EmotionLabel, SentimentAnalysis } from '../../shared/types';
import { DEFAULT_BACKEND_ORIGIN } from './http-analysis-service';
import { HttpSentimentService } from './http-sentiment-service';

const FIXTURE: SentimentAnalysis = {
  videoId: 'dQw4w9WgXcQ',
  status: 'PROCESSED',
  stats: {
    totalComments: 10,
    analyzed: 8,
    skipped: 2,
    positive: 4,
    neutral: 3,
    negative: 1,
    positivePercent: 50,
    neutralPercent: 38,
    negativePercent: 12,
  },
  dataset: {
    collected: 10,
    stored: 10,
    analyzed: 8,
    skipped: 2,
    failed: 0,
    hasMore: false,
    limitReached: false,
  },
  dominantSentiment: 'POSITIVE',
};

const REQUEST = { videoId: 'dQw4w9WgXcQ', pageKind: 'watch' as const };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('HttpSentimentService', () => {
  it('returns success data and calls the documented endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => FIXTURE,
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await new HttpSentimentService().getSentiment(REQUEST);

    expect(result.kind).toBe('success');
    if (result.kind === 'success') {
      expect(result.data.dominantSentiment).toBe('POSITIVE');
      expect(result.data.stats.analyzed).toBe(8);
    }
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      `${DEFAULT_BACKEND_ORIGIN}/api/videos/dQw4w9WgXcQ/sentiment`,
    );
    expect(init.method).toBe('GET');
    // No credentials of any kind are attached.
    expect(JSON.stringify(init.headers ?? {})).not.toMatch(/key|auth|bearer/i);
  });

  it('maps the backend error envelope to categorized results', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({
          error: {
            code: 'storage_unavailable',
            message: 'The dataset store is unavailable right now. Please try again later.',
          },
        }),
      }),
    );

    const result = await new HttpSentimentService().getSentiment(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'storage_unavailable',
      message:
        'The dataset store is unavailable right now. Please try again later.',
    });
  });

  it('rejects malformed success bodies instead of rendering garbage', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ nope: 1 }) }),
    );

    const result = await new HttpSentimentService().getSentiment(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('upstream_data_invalid');
    }
  });

  it('rejects a body without the Sprint 4.1 dataset metrics', async () => {
    const { dataset: _dataset, ...withoutDataset } = FIXTURE;
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => withoutDataset }),
    );

    const result = await new HttpSentimentService().getSentiment(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('upstream_data_invalid');
    }
  });

  it('rejects malformed dataset metrics instead of rendering garbage', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ ...FIXTURE, dataset: { ...FIXTURE.dataset, stored: 'lots' } }),
      }),
    );

    const result = await new HttpSentimentService().getSentiment(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('upstream_data_invalid');
    }
  });

  it('rejects an invalid dominant sentiment value', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ ...FIXTURE, dominantSentiment: 'GANGLORD' }),
      }),
    );

    const result = await new HttpSentimentService().getSentiment(REQUEST);
    expect(result.kind).toBe('error');
    if (result.kind === 'error') {
      expect(result.code).toBe('upstream_data_invalid');
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

    const result = await new HttpSentimentService().getSentiment(REQUEST);
    expect(result).toEqual({
      kind: 'error',
      code: 'acquisition_failed',
      message: 'weird',
    });
  });

  it('reports network failure as an offline backend (friendly copy)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new TypeError('Failed to fetch')),
    );

    const result = await new HttpSentimentService().getSentiment(REQUEST);
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

    const result = await new HttpSentimentService('http://127.0.0.1:8000', 5).getSentiment(
      REQUEST,
    );
    expect(result).toEqual({
      kind: 'error',
      code: 'upstream_timeout',
      message: 'The request timed out. Please try again.',
    });
  });
});

// ---------------------------------------------------------------------------
// Sprint 5: optional audience-intelligence blocks (§7/§15/§25)
// ---------------------------------------------------------------------------

describe('Sprint 5 intelligence blocks', () => {
  const EMOTION_KEYS = [
    'FEAR',
    'ANGER',
    'ANTICIPATION',
    'TRUST',
    'SURPRISE',
    'SADNESS',
    'DISGUST',
    'JOY',
    'NEUTRAL',
  ] as const;

  type ShareMap = Record<EmotionLabel, { count: number; percent: number }>;

  function emotionDistribution(): ShareMap {
    const distribution = Object.fromEntries(
      EMOTION_KEYS.map((key) => [key, { count: 0, percent: 0 }]),
    ) as ShareMap;
    distribution.JOY = { count: 4, percent: 40 };
    distribution.NEUTRAL = { count: 3, percent: 30 };
    distribution.ANTICIPATION = { count: 1, percent: 10 };
    distribution.FEAR = { count: 1, percent: 10 };
    distribution.SADNESS = { count: 1, percent: 10 };
    return distribution;
  }

  const VALID_BODY: SentimentAnalysis = {
    ...FIXTURE,
    stats: { ...FIXTURE.stats, analyzed: 10, totalComments: 10, skipped: 0 },
    dataset: { ...FIXTURE.dataset, analyzed: 10, skipped: 0 },
    emotion: {
      dominant: 'JOY',
      dominantPercent: 40,
      distribution: emotionDistribution(),
    },
    intensity: {
      overall: 'MEDIUM',
      distribution: {
        LOW: { count: 3, percent: 30 },
        MEDIUM: { count: 5, percent: 50 },
        HIGH: { count: 2, percent: 20 },
      },
    },
    confidence: { average: 0.842 },
    audienceMood: 'EXCITED',
  };

  async function send(body: unknown) {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => body }),
    );
    return new HttpSentimentService().getSentiment(REQUEST);
  }

  it('accepts a body with valid intelligence blocks', async () => {
    const result = await send(VALID_BODY);
    expect(result.kind).toBe('success');
    if (result.kind === 'success') {
      expect(result.data.emotion?.dominant).toBe('JOY');
      expect(result.data.intensity?.overall).toBe('MEDIUM');
      expect(result.data.confidence?.average).toBeCloseTo(0.842);
      expect(result.data.audienceMood).toBe('EXCITED');
    }
  });

  it('accepts a legacy body without any Sprint 5 blocks', async () => {
    const { emotion, intensity, confidence, audienceMood, ...legacy } = VALID_BODY;
    void emotion;
    void intensity;
    void confidence;
    void audienceMood;
    const result = await send(legacy);
    expect(result.kind).toBe('success');
  });

  it('accepts explicit nulls (unavailable confidence / too-small mood sample)', async () => {
    const result = await send({
      ...VALID_BODY,
      confidence: { average: null },
      audienceMood: null,
    });
    expect(result.kind).toBe('success');
  });

  it('rejects a dominant emotion outside the real vocabulary', async () => {
    const result = await send({
      ...VALID_BODY,
      emotion: { ...VALID_BODY.emotion!, dominant: 'HAPPINESS' },
    });
    expect(result.kind).toBe('error');
    if (result.kind === 'error') expect(result.code).toBe('upstream_data_invalid');
  });

  it('rejects an emotion distribution missing vocabulary keys', async () => {
    const { JOY: _dropped, ...partial } = emotionDistribution();
    const result = await send({
      ...VALID_BODY,
      emotion: { ...VALID_BODY.emotion!, distribution: partial },
    });
    expect(result.kind).toBe('error');
    if (result.kind === 'error') expect(result.code).toBe('upstream_data_invalid');
  });

  it('rejects an audience mood outside the documented rules', async () => {
    const result = await send({ ...VALID_BODY, audienceMood: 'HYPED' });
    expect(result.kind).toBe('error');
    if (result.kind === 'error') expect(result.code).toBe('upstream_data_invalid');
  });

  it('rejects a non-numeric confidence average', async () => {
    const result = await send({
      ...VALID_BODY,
      confidence: { average: 'very high' },
    });
    expect(result.kind).toBe('error');
    if (result.kind === 'error') expect(result.code).toBe('upstream_data_invalid');
  });

  it('rejects an intensity distribution with garbage shares', async () => {
    const result = await send({
      ...VALID_BODY,
      intensity: {
        overall: 'MEDIUM',
        distribution: {
          LOW: { count: 3, percent: 'lots' },
          MEDIUM: { count: 5, percent: 50 },
          HIGH: { count: 2, percent: 20 },
        },
      },
    });
    expect(result.kind).toBe('error');
    if (result.kind === 'error') expect(result.code).toBe('upstream_data_invalid');
  });
});
