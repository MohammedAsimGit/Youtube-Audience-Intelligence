// @vitest-environment jsdom
/**
 * Sprint 8 §40 - realtime audience intelligence UI tests.
 *
 * Every number below is backend-shaped (camelCase RealtimeStatus): the
 * strip must render exactly what the transport delivered - LIVE/STANDBY/OFF
 * honestly, trend as ARROW + TEXT + delta (never color alone), activity
 * from the backend's own window math - and the store must discard polls
 * that land after navigation. The flow test drives the real polling loop:
 * a `version` flip triggers a SILENT sentiment refetch plus topic/insight
 * refetches, with no Analyze/Refresh action anywhere (§19).
 */
import {
  act,
  cleanup,
  render,
  screen,
} from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  AnalysisService,
  AnalysisServiceRequest,
  AnalysisServiceResult,
} from '../services/analysis/analysis-service';
import type {
  InsightService,
  InsightServiceRequest,
  InsightServiceResult,
} from '../services/analysis/insight-service';
import type {
  RealtimeService,
  RealtimeServiceRequest,
  RealtimeServiceResult,
} from '../services/analysis/realtime-service';
import type {
  SentimentService,
  SentimentServiceRequest,
  SentimentServiceResult,
} from '../services/analysis/sentiment-service';
import { parseVideoContext } from '../services/youtube/video-context';
import type {
  InsightAnalysis,
  OverlayState,
  RealtimeState,
  RealtimeStatus,
  SentimentAnalysis,
  VideoDataResponse,
} from '../shared/types';
import { createStore, type Store } from '../state/store';
import { App } from './App';
import { OverlayBody } from './components/OverlayBody';

const VIDEO_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
const VIDEO = 'dQw4w9WgXcQ';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Fixtures (backend-shaped, synthetic)
// ---------------------------------------------------------------------------

function realtimeFixture(overrides?: Partial<RealtimeStatus>): RealtimeStatus {
  return {
    videoId: VIDEO,
    enabled: true,
    monitoring: true,
    pollIntervalSeconds: 15,
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

function realtimeIdle(): RealtimeState {
  return {
    status: 'idle',
    videoId: null,
    data: null,
    errorCode: null,
    errorMessage: null,
  };
}

function dataFor(videoId: string): VideoDataResponse {
  return {
    video: {
      videoId,
      title: 'Fixture Video Title',
      description: null,
      channelId: 'UC1',
      channelTitle: 'Fixture Channel',
      publishedAt: null,
      categoryId: null,
      duration: null,
      statistics: { viewCount: null, likeCount: null, commentCount: null },
    },
    comments: { items: [], count: 2429, hasMore: false, status: 'ok' },
    source: { provider: 'youtube', retrievedAt: '2026-10-01T00:00:00Z', cached: false },
  };
}

function sentimentBody(videoId: string): SentimentAnalysis {
  return {
    videoId,
    status: 'PROCESSED',
    stats: {
      totalComments: 2429,
      analyzed: 1211,
      skipped: 1218,
      positive: 827,
      neutral: 351,
      negative: 33,
      positivePercent: 68.3,
      neutralPercent: 29,
      negativePercent: 2.7,
    },
    dataset: {
      collected: 2429,
      stored: 2429,
      analyzed: 1211,
      skipped: 1218,
      failed: 0,
      hasMore: false,
      limitReached: false,
    },
    dominantSentiment: 'POSITIVE',
  };
}

function insightBody(videoId: string): InsightAnalysis {
  return {
    videoId,
    status: 'READY',
    message: null,
    headline: 'The audience response is strongly positive',
    summary: 'Derived from analyzed audience data.',
    cards: [],
    sample: { collected: 2429, analyzed: 1211, skipped: 1218 },
    source: 'deterministic',
    provider: { name: 'deterministic', model: null },
    evidenceVersion: '1211:stamp',
    generatedAt: '2026-10-01T12:00:00Z',
    generationMs: 3,
  };
}

/** Full COMPLETE-state overlay body with a given realtime slice. */
function completeState(realtime: RealtimeState): OverlayState {
  return {
    status: 'complete',
    minimized: false,
    activeSection: 'overview',
    videoContext: parseVideoContext(VIDEO_URL),
    notice: null,
    acquisition: {
      status: 'success',
      videoId: VIDEO,
      data: dataFor(VIDEO),
      errorCode: null,
      errorMessage: null,
    },
    sentiment: {
      status: 'success',
      videoId: VIDEO,
      data: sentimentBody(VIDEO),
      errorCode: null,
      errorMessage: null,
    },
    topics: { status: 'idle', videoId: null, data: null, errorCode: null, errorMessage: null },
    insight: { status: 'idle', videoId: null, data: null, errorCode: null, errorMessage: null },
    job: {
      status: 'idle',
      videoId: null,
      jobId: null,
      job: null,
      errorCode: null,
      errorMessage: null,
    },
    realtime,
  };
}

function renderBody(state: OverlayState): void {
  render(
    <OverlayBody
      state={state}
      onRetry={() => {}}
      onSentimentRetry={() => {}}
      onTopicsRetry={() => {}}
      onInsightRetry={() => {}}
    />,
  );
}

const success = (data: RealtimeStatus): RealtimeServiceResult => ({
  kind: 'success',
  data,
});

// ---------------------------------------------------------------------------
// RealtimeStrip rendering
// ---------------------------------------------------------------------------

describe('Sprint 8 - RealtimeStrip rendering', () => {
  it('shows LIVE, the new-count badge, trend label+delta and activity', () => {
    renderBody(completeState({ status: 'success', videoId: VIDEO, data: realtimeFixture(), errorCode: null, errorMessage: null }));

    const strip = screen.getByRole('region', { name: 'Realtime audience monitor' });
    expect(strip.textContent).toContain('LIVE');
    expect(strip.textContent).toContain('+22 new');
    // Trend is arrow + TEXT label + exact delta (never color-only, §21).
    expect(strip.textContent).toContain('▲');
    expect(strip.textContent).toContain('RISING');
    expect(strip.textContent).toContain('+4.2pp');
    // Activity from the backend's own window math (never fabricated).
    expect(strip.textContent).toContain('AUDIENCE MODERATE');
    expect(strip.textContent).toContain('3.4/min');
    expect(strip.textContent).toMatch(/checked/);
  });

  it('renders FALLING with its negative delta and a queued-analysis note', () => {
    renderBody(
      completeState({
        status: 'success',
        videoId: VIDEO,
        data: realtimeFixture({
          trend: { state: 'FALLING', changePp: -6.8, positivePp: -5.4, neutralPp: -1.4, negativePp: 6.8 },
          activity: { level: 'HIGH', newRecent: 47, windowMinutes: 1, ratePerMinute: 7.9 },
          pending: 12,
        }),
        errorCode: null,
        errorMessage: null,
      }),
    );

    const strip = screen.getByRole('region', { name: 'Realtime audience monitor' });
    expect(strip.textContent).toContain('▼');
    expect(strip.textContent).toContain('FALLING');
    expect(strip.textContent).toContain('-6.8pp');
    expect(strip.textContent).toContain('AUDIENCE HIGH');
    expect(strip.textContent).toContain('12 new comments queued for analysis');
  });

  it('renders STABLE honestly, including a below-the-floor delta', () => {
    renderBody(
      completeState({
        status: 'success',
        videoId: VIDEO,
        data: realtimeFixture({
          trend: { state: 'STABLE', changePp: 0.4, positivePp: 0.4, neutralPp: 0, negativePp: -0.4 },
        }),
        errorCode: null,
        errorMessage: null,
      }),
    );
    const strip = screen.getByRole('region', { name: 'Realtime audience monitor' });
    expect(strip.textContent).toContain('STABLE');
    expect(strip.textContent).toContain('+0.4pp');
  });

  it('shows STANDBY when the monitor is not running and OFF when disabled', () => {
    const { unmount } = render(
      <OverlayBody
        state={completeState({ status: 'success', videoId: VIDEO, data: realtimeFixture({ monitoring: false }), errorCode: null, errorMessage: null })}
        onRetry={() => {}}
        onSentimentRetry={() => {}}
        onTopicsRetry={() => {}}
        onInsightRetry={() => {}}
      />,
    );
    expect(screen.getByTestId('realtime-live-pill').textContent).toContain('STANDBY');
    unmount();

    renderBody(
      completeState({
        status: 'success',
        videoId: VIDEO,
        data: realtimeFixture({ enabled: false, monitoring: false }),
        errorCode: null,
        errorMessage: null,
      }),
    );
    expect(screen.getByTestId('realtime-live-pill').textContent).toContain('LIVE UPDATES OFF');
  });

  it('shows first-check-pending before any monitor cycle ran', () => {
    renderBody(
      completeState({
        status: 'success',
        videoId: VIDEO,
        data: realtimeFixture({ lastCheckedAt: null, lastUpdatedAt: null, newComments: 0 }),
        errorCode: null,
        errorMessage: null,
      }),
    );
    expect(screen.getByText('first check pending')).toBeTruthy();
    expect(screen.queryByText(/\+22 new/)).toBeNull();
  });

  it('is absent until a real poll lands (never a placeholder)', () => {
    renderBody(completeState(realtimeIdle()));
    expect(screen.queryByRole('region', { name: 'Realtime audience monitor' })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Realtime store slice
// ---------------------------------------------------------------------------

describe('Sprint 8 - realtime store slice', () => {
  it('applies a snapshot for the current video', () => {
    const store = createStore();
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
      store.updateRealtime(VIDEO, realtimeFixture());
    });
    const state = store.getState().realtime;
    expect(state.status).toBe('success');
    expect(state.data?.version).toBe('2429:1211');
    expect(state.errorCode).toBeNull();
  });

  it('discards a snapshot that lands after navigation (§40)', () => {
    const store = createStore();
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
      store.updateRealtime('BBBBBBBBBBB', realtimeFixture({ videoId: 'BBBBBBBBBBB' }));
    });
    expect(store.getState().realtime.data).toBeNull();
  });

  it('keeps the last known data when a poll fails (§15)', () => {
    const store = createStore();
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
      store.updateRealtime(VIDEO, realtimeFixture());
      store.failRealtime(VIDEO, 'network_error', 'Backend is unreachable.');
    });
    const state = store.getState().realtime;
    expect(state.status).toBe('error');
    expect(state.errorCode).toBe('network_error');
    expect(state.data?.version).toBe('2429:1211'); // strip never blanks
  });

  it('resets on every video-context transition', () => {
    const store = createStore();
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
      store.updateRealtime(VIDEO, realtimeFixture());
    });
    expect(store.getState().realtime.data).not.toBeNull();

    act(() => {
      store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'));
    });
    expect(store.getState().realtime).toEqual(realtimeIdle());

    act(() => {
      store.updateRealtime('BBBBBBBBBBB', realtimeFixture({ videoId: 'BBBBBBBBBBB' }));
      store.setVideoContext(null);
    });
    expect(store.getState().realtime).toEqual(realtimeIdle());
  });

  it('clearRealtime returns the slice to idle', () => {
    const store = createStore();
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
      store.updateRealtime(VIDEO, realtimeFixture());
      store.clearRealtime();
    });
    expect(store.getState().realtime).toEqual(realtimeIdle());
  });
});

// ---------------------------------------------------------------------------
// Overlay polling flow (the §19 no-button guarantee, client side)
// ---------------------------------------------------------------------------

class ScriptedRealtime implements RealtimeService {
  readonly requests: RealtimeServiceRequest[] = [];
  constructor(private readonly versions: string[]) {}
  getRealtime(request: RealtimeServiceRequest): Promise<RealtimeServiceResult> {
    const call = this.requests.length;
    this.requests.push(request);
    const version = this.versions[Math.min(call, this.versions.length - 1)];
    return Promise.resolve(success(realtimeFixture({ version })));
  }
}

class ImmediateAnalysis implements AnalysisService {
  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    return Promise.resolve({ kind: 'success', data: dataFor(request.videoId) });
  }
}

class CountingSentiment implements SentimentService {
  readonly requests: SentimentServiceRequest[] = [];
  getSentiment(request: SentimentServiceRequest): Promise<SentimentServiceResult> {
    this.requests.push(request);
    return Promise.resolve({ kind: 'success', data: sentimentBody(request.videoId) });
  }
}

class CountingInsight implements InsightService {
  readonly requests: InsightServiceRequest[] = [];
  getInsight(request: InsightServiceRequest): Promise<InsightServiceResult> {
    this.requests.push(request);
    return Promise.resolve({ kind: 'success', data: insightBody(request.videoId) });
  }
}

function renderRealtimeFlow({
  realtime,
  sentiment,
  insight,
}: {
  realtime: RealtimeService;
  sentiment: CountingSentiment;
  insight?: InsightService;
}): Store {
  const store = createStore();
  render(
    <App
      store={store}
      analysisService={new ImmediateAnalysis()}
      sentimentService={sentiment}
      insightService={insight}
      realtimeService={realtime}
    />,
  );
  act(() => {
    store.setVideoContext(parseVideoContext(VIDEO_URL));
  });
  return store;
}

describe('Sprint 8 - overlay realtime polling flow', () => {
  it('polls after analysis settles and renders the live strip', async () => {
    vi.useFakeTimers();
    const realtime = new ScriptedRealtime(['2429:1211']);
    const sentiment = new CountingSentiment();
    renderRealtimeFlow({ realtime, sentiment });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });

    expect(realtime.requests.length).toBeGreaterThanOrEqual(1);
    expect(realtime.requests[0].videoId).toBe(VIDEO);
    const strip = screen.getByRole('region', { name: 'Realtime audience monitor' });
    expect(strip.textContent).toContain('LIVE');
  });

  it('turns a version flip into a silent sentiment + insight refetch (§11)', async () => {
    vi.useFakeTimers();
    // First poll establishes the baseline version; the next one flips it,
    // which must re-GET sentiment and clear/refetch the insight slice -
    // all without any user action.
    const realtime = new ScriptedRealtime(['2429:1211', '2451:1233']);
    const sentiment = new CountingSentiment();
    const insight = new CountingInsight();
    renderRealtimeFlow({ realtime, sentiment, insight });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    const sentimentCalls = sentiment.requests.length;
    const insightCalls = insight.requests.length;
    expect(realtime.requests.length).toBe(1);

    // Second poll is scheduled at the clamped interval (15s -> 7.5s).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(realtime.requests.length).toBeGreaterThanOrEqual(2);
    expect(sentiment.requests.length).toBeGreaterThan(sentimentCalls); // silent re-GET
    expect(insight.requests.length).toBeGreaterThan(insightCalls); // auto insight update
    // The strip now carries the flipped version.
    expect(screen.getByRole('region', { name: 'Realtime audience monitor' }).textContent).toContain('LIVE');
  });
});
