// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  AnalysisService,
  AnalysisServiceRequest,
  AnalysisServiceResult,
} from '../services/analysis/analysis-service';
import { UnconfiguredAnalysisService } from '../services/analysis/analysis-service';
import type {
  SentimentService,
  SentimentServiceRequest,
  SentimentServiceResult,
} from '../services/analysis/sentiment-service';
import type {
  AnalysisJobService,
  JobStartRequest,
  JobStartResult,
  JobWaitRequest,
  JobWaitResult,
} from '../services/analysis/analysis-job-service';
import { parseVideoContext } from '../services/youtube/video-context';
import type {
  AnalysisJob,
  SentimentAnalysis,
  VideoDataResponse,
} from '../shared/types';
import { createStore } from '../state/store';
import { App } from './App';

// Auto-cleanup only registers when test globals are enabled; we keep globals
// off, so unmount explicitly between tests.
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** Fake clock incl. Date - the scroll-session logic reads Date.now(). */
function useFakeClock(): void {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
}

/** Scripted service: resolves queued results in order (test fixture only). */
class ScriptedService implements AnalysisService {
  private readonly results: AnalysisServiceResult[];
  public calls: AnalysisServiceRequest[] = [];

  constructor(results: AnalysisServiceResult[]) {
    this.results = results;
  }

  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    this.calls.push(request);
    const next = this.results.shift() ?? {
      kind: 'error' as const,
      code: 'acquisition_failed' as const,
      message: 'no scripted result',
    };
    return Promise.resolve(next);
  }
}

/** Deferred service: the test decides when the in-flight result lands. */
class DeferredService implements AnalysisService {
  public calls: AnalysisServiceRequest[] = [];
  private pending!: (result: AnalysisServiceResult) => void;

  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    this.calls.push(request);
    return new Promise<AnalysisServiceResult>((resolve) => {
      this.pending = resolve;
    });
  }

  resolve(result: AnalysisServiceResult): void {
    this.pending(result);
  }
}

/** Scripted sentiment transport (Sprint 4): queued results in order. */
class ScriptedSentimentService implements SentimentService {
  private readonly results: SentimentServiceResult[];
  public calls: SentimentServiceRequest[] = [];

  constructor(results: SentimentServiceResult[]) {
    this.results = results;
  }

  getSentiment(request: SentimentServiceRequest): Promise<SentimentServiceResult> {
    this.calls.push(request);
    const next = this.results.shift() ?? {
      kind: 'error' as const,
      code: 'acquisition_failed' as const,
      message: 'no scripted sentiment result',
    };
    return Promise.resolve(next);
  }
}

/** Deferred sentiment transport: the test decides when the result lands. */
class DeferredSentimentService implements SentimentService {
  public calls: SentimentServiceRequest[] = [];
  private pending!: (result: SentimentServiceResult) => void;

  getSentiment(request: SentimentServiceRequest): Promise<SentimentServiceResult> {
    this.calls.push(request);
    return new Promise<SentimentServiceResult>((resolve) => {
      this.pending = resolve;
    });
  }

  resolve(result: SentimentServiceResult): void {
    this.pending(result);
  }
}

function acquiredFixture(overrides?: Partial<VideoDataResponse>): VideoDataResponse {
  return {
    video: {
      videoId: 'dQw4w9WgXcQ',
      title: 'Real Fixture Title',
      description: null,
      channelId: 'UC123',
      channelTitle: 'Fixture Channel',
      publishedAt: '2024-03-01T10:00:00Z',
      categoryId: '28',
      duration: null,
      statistics: { viewCount: 1000, likeCount: 50, commentCount: 1250 },
    },
    comments: { items: [], count: 1250, hasMore: false, status: 'ok' },
    source: { provider: 'youtube', retrievedAt: '2024-03-04T00:00:00Z', cached: false },
    ...overrides,
  };
}

function setup(
  service: AnalysisService = new UnconfiguredAnalysisService(0),
  sentimentService?: SentimentService,
  jobService?: AnalysisJobService,
) {
  const store = createStore({
    // Aged context (detected > grace window ago): page scrolls are then
    // eligible for dismissal - fresh contexts sit in the navigation grace.
    videoContext: parseVideoContext(
      'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
      Date.now() - 60_000,
    ),
  });
  const utils = render(
    <App
      store={store}
      analysisService={service}
      sentimentService={sentimentService}
      jobService={jobService}
    />,
  );
  return { store, ...utils };
}

/** Renders with scripted services and opens the overlay in one step. */
function setupWith(
  service: AnalysisService,
  sentimentService?: SentimentService,
  jobService?: AnalysisJobService,
) {
  const result = setup(service, sentimentService, jobService);
  fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
  return result;
}

/** TEST FIXTURE: backend-shaped aggregates (never presented as real data). */
function sentimentFixture(
  overrides?: Partial<SentimentAnalysis>,
): SentimentAnalysis {
  const fixture: SentimentAnalysis = {
    videoId: 'dQw4w9WgXcQ',
    status: 'PROCESSED',
    stats: {
      totalComments: 2400,
      analyzed: 2400,
      skipped: 0,
      positive: 1680,
      neutral: 480,
      negative: 240,
      positivePercent: 70,
      neutralPercent: 20,
      negativePercent: 10,
    },
    dataset: {
      collected: 2400,
      stored: 2400,
      analyzed: 2400,
      skipped: 0,
      failed: 0,
      hasMore: false,
      limitReached: false,
    },
    dominantSentiment: 'POSITIVE',
    ...overrides,
  };
  // When a test overrides stats, keep the dataset block reconciled with them
  // (collected/stored/analyzed/skipped must match what the backend would send).
  if (overrides?.stats !== undefined && overrides?.dataset === undefined) {
    fixture.dataset = {
      ...fixture.dataset,
      collected: fixture.stats.totalComments,
      stored: fixture.stats.totalComments,
      analyzed: fixture.stats.analyzed,
      skipped: fixture.stats.skipped,
    };
  }
  return fixture;
}

/** TEST FIXTURE: a dataset with zero comments (empty analysis state). */
function emptySentimentFixture(): SentimentAnalysis {
  return sentimentFixture({
    status: 'NOT_ANALYZED',
    stats: {
      totalComments: 0,
      analyzed: 0,
      skipped: 0,
      positive: 0,
      neutral: 0,
      negative: 0,
      positivePercent: 0,
      neutralPercent: 0,
      negativePercent: 0,
    },
    dominantSentiment: null,
  });
}

describe('App overlay lifecycle', () => {
  it('shows the FAB when closed and opens the overlay on click', async () => {
    setup();
    const fab = screen.getByRole('button', { name: /open ai analyzer/i });
    fireEvent.click(fab);
    const dialog = screen.getByRole('dialog', { name: /ai analyzer overlay/i });
    expect(dialog).toBeTruthy();
    // Sprint 4.4: the automatic flow runs on open and, with no services
    // configured, settles honestly back to READY (no fabricated results).
    await waitFor(() => {
      expect(screen.getAllByText('READY').length).toBeGreaterThan(0);
    });
    expect(screen.getByText('dQw4w9WgXcQ')).toBeTruthy();
    // §3: no Analyze control exists anywhere in the overlay.
    expect(screen.queryByRole('button', { name: /^analyze$/i })).toBeNull();
    // §53: the ready context card carries the ACTIVE VIDEO marker.
    expect(screen.getAllByText('ACTIVE VIDEO').length).toBeGreaterThan(0);
  });

  it('closes the overlay and returns the FAB', () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    fireEvent.click(screen.getByRole('button', { name: /close ai analyzer/i }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('button', { name: /open ai analyzer/i })).toBeTruthy();
  });

  it('minimizes to the compact panel without losing state, then restores', async () => {
    const { store } = setup();
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    // Sprint 4.4: let the automatic flow settle before probing panel state.
    await waitFor(() => expect(store.getState().status).toBe('ready'));
    store.setNotice('state probe');

    fireEvent.click(screen.getByRole('button', { name: /minimize ai analyzer panel/i }));
    // Compact panel: body hidden, restore control present, state preserved.
    expect(screen.queryByText('state probe')).toBeNull();
    expect(store.getState().minimized).toBe(true);
    expect(store.getState().status).toBe('ready');
    expect(store.getState().notice).toBe('state probe');
    expect(screen.getByRole('button', { name: /restore ai analyzer panel/i })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /restore ai analyzer panel/i }));
    expect(store.getState().minimized).toBe(false);
    expect(screen.getByText('state probe')).toBeTruthy();
  });

  it('closes on Escape', () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('runs the honest analyze flow: no fabricated results, explicit notice', async () => {
    setup();
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.

    // Brief analyzing state, then back to ready with an honest notice.
    await waitFor(() => {
      expect(screen.getByText(/no results were generated/i)).toBeTruthy();
    });
    expect(screen.getAllByText('READY').length).toBeGreaterThan(0);
    // No fake sentiment numbers anywhere in the overlay.
    expect(screen.queryByText(/positive\s*7/i)).toBeNull();
    expect(screen.queryByText(/negative/i)).toBeNull();
  });

  it('renders the unsupported-page state on non-video YouTube pages', () => {
    const store = createStore({
      videoContext: parseVideoContext('https://www.youtube.com/results?search_query=cats'),
    });
    render(<App store={store} analysisService={new UnconfiguredAnalysisService(0)} />);
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    expect(screen.getByText('NOT AVAILABLE')).toBeTruthy();
    expect(screen.getByText(/not currently supported/i)).toBeTruthy();
  });

  it('renders the video-detection-failure state for watch pages without a valid id', () => {
    const store = createStore({
      videoContext: parseVideoContext('https://www.youtube.com/watch?v=bad'),
    });
    render(<App store={store} analysisService={new UnconfiguredAnalysisService(0)} />);
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    expect(screen.getByText(/unable to identify the current video/i)).toBeTruthy();
  });
});

describe('App acquisition flow (Sprint 2)', () => {
  it('shows LOADING while acquiring, then DATA ACQUIRED with real facts', async () => {
    const service = new ScriptedService([{ kind: 'success', data: acquiredFixture() }]);
    setupWith(service);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.

    // Honest loading state - no fake progress percentages anywhere.
    expect(screen.getByText('LOADING')).toBeTruthy();
    expect(screen.getByText(/acquiring data/i)).toBeTruthy();
    expect(screen.getByText(/collecting audience responses/i)).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByText('DATA ACQUIRED')).toBeTruthy();
    });
    expect(screen.getByText('Real Fixture Title')).toBeTruthy();
    expect(screen.getByText('1,250')).toBeTruthy();
    // Truthful dataset status: no sentiment service was configured, so the
    // backend has not reported an analysis state yet (§24).
    expect(screen.getByText('Awaiting analysis')).toBeTruthy();
    // The video id was sent to the service.
    expect(service.calls[0].videoId).toBe('dQw4w9WgXcQ');
    // No fabricated sentiment anywhere.
    expect(screen.queryByText(/positive/i)).toBeNull();
    expect(screen.queryByText(/negative/i)).toBeNull();
  });

  it('shows categorized acquisition error; Retry re-runs automatically (§23)', async () => {
    const service = new ScriptedService([
      {
        kind: 'error',
        code: 'quota_exceeded',
        message: 'The YouTube API quota is exhausted for now. Please try again later.',
      },
      { kind: 'success', data: acquiredFixture() },
    ]);
    setupWith(service);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('QUOTA EXCEEDED')).toBeTruthy();
    });
    expect(
      screen.getByText(/quota is exhausted for now/i),
    ).toBeTruthy();
    expect(screen.getByText('ERROR')).toBeTruthy();

    // §23: Retry (error-state only) restarts the pipeline by itself.
    fireEvent.click(screen.getByRole('button', { name: /^retry$/i }));
    await waitFor(() => {
      expect(screen.getByText('DATA ACQUIRED')).toBeTruthy();
    });
    expect(service.calls).toHaveLength(2);
    expect(screen.queryByRole('button', { name: /^analyze$/i })).toBeNull();
  });

  it('keeps the overlay open for the new video and clears stale data', async () => {
    const service = new ScriptedService([{ kind: 'success', data: acquiredFixture() }]);
    const { store } = setupWith(service);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('Real Fixture Title')).toBeTruthy();
    });

    // Simulate SPA navigation to video B (store handles the reset).
    act(() => {
      store.setVideoContext(
        parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
      );
    });

    // Overlay stays open for video B and the automatic flow RESTARTS for
    // it (§17) - Video A's data can never leak into Video B's context.
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /open ai analyzer/i })).toBeNull();
    expect(screen.queryByText('Real Fixture Title')).toBeNull();
    expect(screen.queryByText('DATA ACQUIRED')).toBeNull();
    expect(store.getState().videoContext?.videoId).toBe('BBBBBBBBBBB');
    expect(store.getState().acquisition.status).toBe('loading'); // auto-restarted
    expect(store.getState().acquisition.videoId).toBe('BBBBBBBBBBB');
    expect(store.getState().notice).toBeNull();
    expect(store.getState().status).toBe('loading');
    expect(store.getState().videoContext?.videoId).toBe('BBBBBBBBBBB');
    // Exactly one new run, bound to video B.
    await waitFor(() => expect(service.calls).toHaveLength(2));
    expect(service.calls[1].videoId).toBe('BBBBBBBBBBB');
  });

  it('shows explicit COMMENTS UNAVAILABLE instead of inventing comments', async () => {
    const service = new ScriptedService([
      {
        kind: 'success',
        data: acquiredFixture({
          comments: { items: [], count: 0, hasMore: false, status: 'none' },
        }),
      },
    ]);
    setupWith(service);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('COMMENTS UNAVAILABLE')).toBeTruthy();
    });
    expect(
      screen.getByText(/no accessible audience comments/i),
    ).toBeTruthy();
    expect(screen.getByText('Awaiting analysis')).toBeTruthy();
  });

  it('handles comments-disabled as an explicit non-error state', async () => {
    const service = new ScriptedService([
      {
        kind: 'success',
        data: acquiredFixture({
          comments: { items: [], count: 0, hasMore: false, status: 'disabled' },
        }),
      },
    ]);
    setupWith(service);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('COMMENTS UNAVAILABLE')).toBeTruthy();
    });
    expect(screen.getByText(/comments are disabled for this video/i)).toBeTruthy();
    expect(screen.queryByText('ERROR')).toBeNull(); // not an error state
  });
});

describe('scroll-aware overlay dismissal', () => {
  it('a quick flick does not close the overlay (sustained scroll required)', async () => {
    useFakeClock();
    const { store } = setup();
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();

    // A: short burst of YouTube page scrolling, then the user stops.
    fireEvent.scroll(document);
    fireEvent.scroll(document.body);
    act(() => {
      vi.advanceTimersByTime(400); // idle gap → scroll session cancelled
      vi.advanceTimersByTime(3000); // well past the 1 s checkpoint
    });
    // Let the automatic flow's stub response settle (Sprint 4.4).
    await act(async () => {});

    // Little scrolling must NOT dismiss.
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    expect(store.getState().status).toBe('ready');
  });

  it('minimizes after ~1s of sustained page scrolling (never closes)', () => {
    useFakeClock();
    const { store } = setup();
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();

    // Slices must survive minimization untouched (§10: no restart/refetch).
    const acquisitionBefore = store.getState().acquisition;
    const sentimentBefore = store.getState().sentiment;
    const jobBefore = store.getState().job;
    const contextBefore = store.getState().videoContext;

    // Keep scrolling continuously for a full second.
    act(() => {
      for (let elapsed = 0; elapsed <= 1000; elapsed += 100) {
        vi.advanceTimersByTime(100);
        fireEvent.scroll(document);
      }
    });

    // OPEN → MINIMIZED (never CLOSED): the compact pill stays mounted and
    // the FAB-equivalent restore target remains visible.
    expect(store.getState().minimized).toBe(true);
    expect(store.getState().status).not.toBe('closed');
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /restore ai analyzer panel/i })).toBeTruthy();
    // Nothing else moved: same slice objects, same video context.
    expect(store.getState().acquisition).toBe(acquisitionBefore);
    expect(store.getState().sentiment).toBe(sentimentBefore);
    expect(store.getState().job).toBe(jobBefore);
    expect(store.getState().videoContext).toBe(contextBefore);

    // Already minimized → further scrolls are no-ops (no repeated updates).
    const stateAfterMinimize = store.getState();
    fireEvent.scroll(document);
    fireEvent.scroll(document.body);
    expect(store.getState()).toBe(stateAfterMinimize);

    // Clicking the pill restores MINIMIZED → OPEN with everything intact.
    fireEvent.click(screen.getByRole('button', { name: /restore ai analyzer panel/i }));
    expect(store.getState().minimized).toBe(false);
    expect(store.getState().status).not.toBe('closed');
    expect(screen.queryByRole('button', { name: /restore ai analyzer panel/i })).toBeNull();
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
  });

  it('keeps the overlay open when its own content scrolls', async () => {
    useFakeClock();
    const { store } = setup();
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));

    // B: overlay-internal scroll (scrollable analysis body) must not dismiss.
    const body = document.querySelector('.sai-body');
    expect(body).toBeTruthy();
    fireEvent.scroll(body!);
    act(() => {
      vi.advanceTimersByTime(5000); // no scroll session is ever started
    });
    // Let the automatic flow's stub response settle (Sprint 4.4).
    await act(async () => {});

    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    expect(store.getState().status).toBe('ready');
  });

  it('does nothing on scroll while the overlay is minimized', async () => {
    const { store } = setup();
    fireEvent.click(screen.getByRole('button', { name: /open ai analyzer/i }));
    // Sprint 4.4: let the automatic flow settle before probing panel state.
    await waitFor(() => expect(store.getState().status).toBe('ready'));
    fireEvent.click(screen.getByRole('button', { name: /minimize ai analyzer panel/i }));

    fireEvent.scroll(document);

    expect(store.getState().minimized).toBe(true);
    expect(store.getState().status).toBe('ready');
    expect(screen.getByRole('button', { name: /restore ai analyzer panel/i })).toBeTruthy();
  });

  it('scrolling while closed does nothing (FAB stays, no state churn)', () => {
    const { store } = setup();
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.scroll(document);

    expect(store.getState().status).toBe('closed');
    expect(screen.getByRole('button', { name: /open ai analyzer/i })).toBeTruthy();
  });
});

describe('race conditions (video identity protection)', () => {
  it('does not reopen the overlay when a result lands after it was dismissed', async () => {
    const service = new DeferredService();
    const { store } = setupWith(service);
    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    expect(store.getState().acquisition.status).toBe('loading');

    // User dismisses the overlay (✕) while the request is in flight.
    fireEvent.click(screen.getByRole('button', { name: /close ai analyzer/i }));
    expect(screen.queryByRole('dialog')).toBeNull();

    await act(async () => {
      service.resolve({ kind: 'success', data: acquiredFixture() });
    });

    // The result must not silently reopen the dismissed overlay...
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('button', { name: /open ai analyzer/i })).toBeTruthy();
    expect(store.getState().status).toBe('closed');
    // ...but the data (same video) is kept so reopening avoids a refetch.
    expect(store.getState().acquisition.status).toBe('success');
  });

  it("discards Video A's response after navigating to Video B (Test G)", async () => {
    // Per-call resolvers so video A's promise stays resolvable after B's
    // automatic run starts (Sprint 4.4 §17).
    const resolvers: Array<(r: AnalysisServiceResult) => void> = [];
    const calls: AnalysisServiceRequest[] = [];
    const tracked: AnalysisService = {
      analyze: (request) => {
        calls.push(request);
        return new Promise<AnalysisServiceResult>((resolve) => {
          resolvers.push(resolve);
        });
      },
    };
    const { store } = setupWith(tracked);
    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].videoId).toBe('dQw4w9WgXcQ');

    // Navigate to video B while video A's request is still in flight.
    act(() => {
      store.setVideoContext(
        parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
      );
    });
    // Overlay stays open for video B and B's own automatic run started.
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].videoId).toBe('BBBBBBBBBBB');

    // Video A's response lands late...
    await act(async () => {
      resolvers[0]({ kind: 'success', data: acquiredFixture() });
    });

    // ...it cannot overwrite Video B's in-flight state.
    expect(store.getState().acquisition.status).toBe('loading'); // B acquiring
    expect(store.getState().acquisition.videoId).toBe('BBBBBBBBBBB');
    expect(store.getState().acquisition.data).toBeNull();
    expect(store.getState().status).not.toBe('complete');
    expect(screen.queryByText('Real Fixture Title')).toBeNull();
    expect(store.getState().videoContext?.videoId).toBe('BBBBBBBBBBB');
  });

  it('keeps Video B when Video A\'s slower response lands after B resolved (§48)', async () => {
    // Two in-flight acquisition requests resolved OUT of order: B first,
    // then A - the UI must show B and never revert to A.
    const resolvers: Array<(r: AnalysisServiceResult) => void> = [];
    const calls: AnalysisServiceRequest[] = [];
    const outOfOrder: AnalysisService = {
      analyze: (request) => {
        calls.push(request);
        return new Promise<AnalysisServiceResult>((resolve) => {
          resolvers.push(resolve);
        });
      },
    };
    const { store } = setupWith(outOfOrder);

    // Request A starts on video A.
    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].videoId).toBe('dQw4w9WgXcQ');

    // Navigate to video B; request B starts while A is still pending.
    act(() => {
      store.setVideoContext(
        parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
      );
    });
    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].videoId).toBe('BBBBBBBBBBB');

    // B resolves first and renders.
    const dataB = acquiredFixture({
      video: {
        ...acquiredFixture().video,
        videoId: 'BBBBBBBBBBB',
        title: 'Video B Title',
      },
      comments: { items: [], count: 350, hasMore: false, status: 'ok' },
    });
    await act(async () => {
      resolvers[1]({ kind: 'success', data: dataB });
    });
    expect(screen.getByText('Video B Title')).toBeTruthy();
    expect(screen.getByText('350')).toBeTruthy();

    // A's stale response arrives afterwards - it must be discarded.
    await act(async () => {
      resolvers[0]({ kind: 'success', data: acquiredFixture() });
    });
    expect(screen.getByText('Video B Title')).toBeTruthy();
    expect(screen.queryByText('Real Fixture Title')).toBeNull();
    expect(screen.queryByText('1,250')).toBeNull();
    expect(store.getState().videoContext?.videoId).toBe('BBBBBBBBBBB');
  });

  it('shows a subtle ACTIVE VIDEO marker for the working dataset (§53)', async () => {
    const service = new ScriptedService([{ kind: 'success', data: acquiredFixture() }]);
    setupWith(service);
    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('Real Fixture Title')).toBeTruthy();
    });
    // Quiet ownership marker on the working dataset, never dominant (the
    // ready context card carries the same marker - covered above).
    const markers = screen.getAllByText('ACTIVE VIDEO');
    expect(markers.length).toBeGreaterThan(0);
    markers.forEach((marker) => {
      expect(marker.className).toContain('sai-active-badge');
    });
  });
});

describe('sentiment intelligence (Sprint 4)', () => {
  it('shows the analyzing pipeline while the sentiment request is in flight', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new DeferredSentimentService();
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.

    // Acquisition lands first; the sentiment window renders the pipeline.
    await waitFor(() => {
      expect(screen.getByText('ANALYZING AUDIENCE')).toBeTruthy();
    });
    expect(screen.getByText('ANALYZING')).toBeTruthy(); // status chip
    expect(screen.getByText('Reading discussion')).toBeTruthy();
    expect(screen.getByText('Processing comments')).toBeTruthy();
    expect(screen.getByText('Classifying sentiment')).toBeTruthy();
    expect(screen.getByText('Aggregating results')).toBeTruthy();
    // Acquired facts stay visible during analysis.
    expect(screen.getByText('Real Fixture Title')).toBeTruthy();
    // No fabricated results while loading.
    expect(screen.queryByText('70%')).toBeNull();
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();

    await act(async () => {
      sentiment.resolve({ kind: 'success', data: sentimentFixture() });
    });
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    expect(sentiment.calls[0].videoId).toBe('dQw4w9WgXcQ');
  });

  it('renders dominant sentiment, distribution and coverage from backend aggregates', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: sentimentFixture() },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });

    const distribution = within(
      screen.getByRole('region', { name: 'Sentiment distribution' }),
    );
    expect(distribution.getByText('POSITIVE')).toBeTruthy(); // dominant verdict
    expect(distribution.getAllByText('70%').length).toBeGreaterThan(0);
    expect(distribution.getByText('20%')).toBeTruthy();
    expect(distribution.getByText('10%')).toBeTruthy();
    expect(distribution.getByText('1,680')).toBeTruthy();
    expect(distribution.getByText('480')).toBeTruthy();
    expect(distribution.getByText('240')).toBeTruthy();
    // Labels, not color alone.
    expect(distribution.getByText('Positive')).toBeTruthy();
    expect(distribution.getByText('Neutral')).toBeTruthy();
    expect(distribution.getByText('Negative')).toBeTruthy();

    const coverage = within(
      screen.getByRole('region', { name: 'Analysis coverage' }),
    );
    // §12: analyzed counter (2,400) + collected counter + labels.
    expect(coverage.getByText('2,400')).toBeTruthy();
    expect(coverage.getByText('comments collected')).toBeTruthy();
    expect(coverage.getByText('analyzed')).toBeTruthy();
    // §28: the denominator is explicit next to the percentages.
    expect(screen.getByText(/Based on 2,400 analyzed comments/i)).toBeTruthy();
    // §24: the status row now truthfully reflects the completed analysis
    // (the old static "READY FOR AI ANALYSIS" next to "ANALYSIS COMPLETE"
    // contradiction is gone).
    expect(screen.getByText('Analysis complete')).toBeTruthy();
  });

  it('renders NO AUDIENCE DATA when the dataset has no comments', async () => {
    const acquisition = new ScriptedService([
      {
        kind: 'success',
        data: acquiredFixture({
          comments: { items: [], count: 0, hasMore: false, status: 'none' },
        }),
      },
    ]);
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: emptySentimentFixture() },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('NO AUDIENCE DATA')).toBeTruthy();
    });
    expect(
      screen.getByText(/no comments available for analysis/i),
    ).toBeTruthy();
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
    expect(screen.queryByText('70%')).toBeNull();
  });

  it('renders AWAITING ANALYSIS with no Run-analysis control (§3/§22)', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const notAnalyzed = sentimentFixture({
      status: 'NOT_ANALYZED',
      stats: {
        totalComments: 1250,
        analyzed: 0,
        skipped: 0,
        positive: 0,
        neutral: 0,
        negative: 0,
        positivePercent: 0,
        neutralPercent: 0,
        negativePercent: 0,
      },
      dominantSentiment: null,
    });
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: notAnalyzed },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('AWAITING ANALYSIS')).toBeTruthy();
    });
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();

    // §3/§22: the happy path never offers a manual analysis action -
    // results only arrive through the automatic flow.
    expect(screen.queryByRole('button', { name: /run analysis/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /^analyze/i })).toBeNull();
    expect(sentiment.calls).toHaveLength(1);
  });

  it('maps a sentiment API error to ANALYSIS UNAVAILABLE and retries', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new ScriptedSentimentService([
      {
        kind: 'error',
        code: 'storage_unavailable',
        message: 'The dataset store is unavailable right now. Please try again later.',
      },
      { kind: 'success', data: sentimentFixture() },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS UNAVAILABLE')).toBeTruthy();
    });
    expect(
      screen.getByText(/dataset store is unavailable right now/i),
    ).toBeTruthy();
    // Acquisition facts survive a sentiment failure.
    expect(screen.getByText('Real Fixture Title')).toBeTruthy();
    expect(screen.getByText('DATA ACQUIRED')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /^retry$/i }));
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    expect(sentiment.calls).toHaveLength(2);
  });

  it('maps a network failure to AI SERVICE OFFLINE', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new ScriptedSentimentService([
      {
        kind: 'error',
        code: 'network_error',
        message: 'Backend is unreachable. Start the local backend (see README) and try again.',
      },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('AI SERVICE OFFLINE')).toBeTruthy();
    });
    expect(screen.getByText(/check your connection and try again/i)).toBeTruthy();
    // Never a raw stack trace.
    expect(screen.queryByText(/traceback|at Object\.|httpx/i)).toBeNull();
  });

  it('keeps the pipeline visible while the backend reports PROCESSING', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: sentimentFixture({ status: 'PROCESSING' }) },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYZING AUDIENCE')).toBeTruthy();
    });
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
    expect(screen.queryByText('70%')).toBeNull();
  });

  it('renders ANALYSIS UNAVAILABLE for a backend FAILED state', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new ScriptedSentimentService([
      {
        kind: 'success',
        data: sentimentFixture({
          status: 'FAILED',
          stats: {
            totalComments: 2400,
            analyzed: 0,
            skipped: 0,
            positive: 0,
            neutral: 0,
            negative: 0,
            positivePercent: 0,
            neutralPercent: 0,
            negativePercent: 0,
          },
          dominantSentiment: null,
        }),
      },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS UNAVAILABLE')).toBeTruthy();
    });
    expect(screen.getByRole('button', { name: /^retry$/i })).toBeTruthy();
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
  });

  it('renders INSUFFICIENT LANGUAGE SUPPORT when every row was skipped', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new ScriptedSentimentService([
      {
        kind: 'success',
        data: sentimentFixture({
          stats: {
            totalComments: 1200,
            analyzed: 0,
            skipped: 1200,
            positive: 0,
            neutral: 0,
            negative: 0,
            positivePercent: 0,
            neutralPercent: 0,
            negativePercent: 0,
          },
          dominantSentiment: null,
        }),
      },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('INSUFFICIENT LANGUAGE SUPPORT')).toBeTruthy();
    });
    expect(
      screen.getByText(/could not be analyzed reliably/i),
    ).toBeTruthy();
    expect(screen.getByText(/1,200 stored comments are in languages/i)).toBeTruthy();
    // §29: exact counts instead of a fake 0% distribution.
    expect(
      screen.getByText('Collected 1,200 · Analyzed 0 · Skipped 1,200'),
    ).toBeTruthy();
    // Skipped rows are never presented as an analyzed distribution.
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
  });

  it('refresh re-requests sentiment and replaces stale aggregates', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const refreshed = sentimentFixture({
      videoId: 'dQw4w9WgXcQ',
      stats: {
        totalComments: 600,
        analyzed: 600,
        skipped: 0,
        positive: 120,
        neutral: 300,
        negative: 180,
        positivePercent: 20,
        neutralPercent: 50,
        negativePercent: 30,
      },
      dominantSentiment: 'NEUTRAL',
    });
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: sentimentFixture() },
      { kind: 'success', data: refreshed },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    expect(screen.getByText('2,400')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /refresh analysis/i }));
    await waitFor(() => {
      expect(screen.getByText('600')).toBeTruthy();
    });
    expect(sentiment.calls).toHaveLength(2);
    expect(screen.getByText('NEUTRAL')).toBeTruthy();
    expect(screen.queryByText('2,400')).toBeNull(); // old aggregates replaced
  });

  it("discards Video A's sentiment after navigating to Video B (stale protection)", async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
      {
        kind: 'success',
        data: acquiredFixture({
          video: { ...acquiredFixture().video, videoId: 'BBBBBBBBBBB', title: 'Video B' },
        }),
      },
    ]);
    // Per-call resolvers: video A's sentiment promise must stay resolvable
    // after video B's automatic run issues its own request (§17).
    const resolvers: Array<(r: SentimentServiceResult) => void> = [];
    const sentimentCalls: SentimentServiceRequest[] = [];
    const sentiment: SentimentService = {
      getSentiment: (request) => {
        sentimentCalls.push(request);
        return new Promise<SentimentServiceResult>((resolve) => {
          resolvers.push(resolve);
        });
      },
    };
    const { store } = setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(sentimentCalls).toHaveLength(1);
    });
    expect(sentimentCalls[0].videoId).toBe('dQw4w9WgXcQ');
    expect(store.getState().sentiment.status).toBe('loading');

    // Navigate while Video A's sentiment request is still in flight.
    act(() => {
      store.setVideoContext(
        parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
      );
    });
    // Video B's own automatic run requests B's sentiment with B's id.
    await waitFor(() => {
      expect(sentimentCalls).toHaveLength(2);
    });
    expect(sentimentCalls[1].videoId).toBe('BBBBBBBBBBB');

    // Video A's 70% lands late...
    await act(async () => {
      resolvers[0]({ kind: 'success', data: sentimentFixture() });
    });

    // ...it must never appear under Video B (B is still analyzing).
    expect(store.getState().sentiment.videoId).toBe('BBBBBBBBBBB');
    expect(store.getState().sentiment.status).toBe('loading');
    expect(store.getState().sentiment.data).toBeNull();
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
    expect(screen.queryByText('70%')).toBeNull();
    expect(screen.queryByText('POSITIVE')).toBeNull();

    // Video B's own result then lands for B.
    await act(async () => {
      resolvers[1]({
        kind: 'success',
        data: sentimentFixture({ videoId: 'BBBBBBBBBBB' }),
      });
    });
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    expect(sentimentCalls[1].videoId).toBe('BBBBBBBBBBB');
  });
});

describe('video navigation auto-open (click a video → overlay)', () => {
  it('auto-opens AND auto-starts analysis for the new video - no click (§4)', async () => {
    const service = new DeferredService();
    const { store } = setup(service); // closed, FAB visible on video A
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(service.calls).toHaveLength(0); // nothing runs on a non-video page

    act(() => {
      store.setVideoContext(
        parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
      );
    });

    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /open ai analyzer/i })).toBeNull();
    expect(store.getState().videoContext?.videoId).toBe('BBBBBBBBBBB');
    // Video selection is the trigger: the run started with zero clicks.
    await waitFor(() => expect(service.calls).toHaveLength(1));
    expect(service.calls[0].videoId).toBe('BBBBBBBBBBB');
    expect(store.getState().acquisition.status).toBe('loading');
    expect(store.getState().status).toBe('loading');
    // §3: no Analyze control exists in the overlay.
    expect(screen.queryByRole('button', { name: /^analyze$/i })).toBeNull();
  });

  it('ignores the page scroll YouTube fires during SPA navigation (grace)', () => {
    const { store } = setup();
    act(() => {
      store.setVideoContext(
        // Fresh detectedAt → inside the navigation grace window.
        parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
      );
    });

    // YouTube scrolls the page to top while opening the new video.
    fireEvent.scroll(document);
    fireEvent.scroll(document.body);

    // The overlay stays open for the new video (auto-analysis is running).
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    expect(store.getState().status).not.toBe('closed');
    expect(store.getState().videoContext?.videoId).toBe('BBBBBBBBBBB');
  });
});

describe('Sprint 4.1 - accurate audience metrics', () => {
  /**
   * §41 controlled dataset: 500 stored / 400 analyzed / 100 skipped with a
   * 220/130/50 split. The percentages MUST use 400 as the denominator
   * (55% / 32.5% / 12.5%) - never 500 (44% / 26% / 10%).
   */
  function accuracyFixture(): SentimentAnalysis {
    return sentimentFixture({
      stats: {
        totalComments: 500,
        analyzed: 400,
        skipped: 100,
        positive: 220,
        neutral: 130,
        negative: 50,
        positivePercent: 55,
        neutralPercent: 32.5,
        negativePercent: 12.5,
      },
      dominantSentiment: 'POSITIVE',
    });
  }

  it('reconciles collected / analyzed / skipped with analyzed as denominator', async () => {
    const acquisition = new ScriptedService([
      {
        kind: 'success',
        data: acquiredFixture({
          comments: { items: [], count: 500, hasMore: false, status: 'ok' },
        }),
      },
    ]);
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: accuracyFixture() },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });

    // AUDIENCE RESPONSES: exact collected / analyzed / skipped counts.
    expect(screen.getByText('500')).toBeTruthy(); // "500 comments collected"
    const breakdown = within(screen.getByRole('list', { name: 'Dataset breakdown' }));
    expect(breakdown.getByText('400')).toBeTruthy();
    expect(breakdown.getByText('analyzed')).toBeTruthy();
    expect(breakdown.getByText('100')).toBeTruthy();
    expect(
      breakdown.getByText('skipped (language not supported)'),
    ).toBeTruthy();

    // Sentiment card: one-decimal percentages beside exact counts.
    const distribution = within(
      screen.getByRole('region', { name: 'Sentiment distribution' }),
    );
    expect(distribution.getAllByText('55%').length).toBeGreaterThan(0);
    expect(distribution.getByText('32.5%')).toBeTruthy();
    expect(distribution.getByText('12.5%')).toBeTruthy();
    expect(distribution.getByText('220')).toBeTruthy();
    expect(distribution.getByText('130')).toBeTruthy();
    expect(distribution.getByText('50')).toBeTruthy();

    // The denominator is visible (§28).
    expect(screen.getByText(/Based on 400 analyzed comments/i)).toBeTruthy();

    // Wrong-denominator rendering must never appear (§41).
    expect(screen.queryByText('44%')).toBeNull();
    expect(screen.queryByText('26%')).toBeNull();
    expect(screen.queryByText('10%')).toBeNull();
  });

  it('renders large comment counts with thousands separators', async () => {
    const acquisition = new ScriptedService([
      {
        kind: 'success',
        data: acquiredFixture({
          comments: { items: [], count: 5000, hasMore: false, status: 'ok' },
        }),
      },
    ]);
    setupWith(acquisition);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('5,000')).toBeTruthy();
    });
    expect(screen.getByText('comments collected')).toBeTruthy();
  });

  it('claims "all available acquired" only when hasMore is false', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: sentimentFixture() },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    expect(screen.getByText(/All available comments acquired/i)).toBeTruthy();
    expect(screen.queryByText(/Acquisition limit reached/i)).toBeNull();
    expect(screen.queryByText(/more may be available/i)).toBeNull();
  });

  it('claims the configured limit only when the backend reports limitReached', async () => {
    const acquisition = new ScriptedService([
      {
        kind: 'success',
        data: acquiredFixture({
          comments: { items: [], count: 5000, hasMore: true, status: 'ok' },
        }),
      },
    ]);
    const sentiment = new ScriptedSentimentService([
      {
        kind: 'success',
        data: sentimentFixture({
          dataset: {
            collected: 5000,
            stored: 5000,
            analyzed: 5000,
            skipped: 0,
            failed: 0,
            hasMore: true,
            limitReached: true,
          },
        }),
      },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    expect(
      screen.getByText(
        'Acquisition limit reached - 5,000 collected, more comments may be available.',
      ),
    ).toBeTruthy();
  });

  it('reports more-available WITHOUT a limit claim when limitReached is false', async () => {
    const acquisition = new ScriptedService([
      {
        kind: 'success',
        data: acquiredFixture({
          comments: { items: [], count: 200, hasMore: true, status: 'ok' },
        }),
      },
    ]);
    const sentiment = new ScriptedSentimentService([
      {
        kind: 'success',
        data: sentimentFixture({
          dataset: {
            collected: 200,
            stored: 200,
            analyzed: 200,
            skipped: 0,
            failed: 0,
            hasMore: true,
            limitReached: false,
          },
        }),
      },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    // Truthful: more exist, but the configured limit was not the reason.
    expect(
      screen.getByText('More comments may be available from YouTube.'),
    ).toBeTruthy();
    expect(screen.queryByText(/Acquisition limit reached/i)).toBeNull();
  });

  it('status row follows the backend lifecycle (PROCESSING / FAILED)', async () => {
    // PROCESSING with real backend progress counts (§30 - no fabrication).
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
      { kind: 'success', data: acquiredFixture() },
    ]);
    const processing = sentimentFixture({
      status: 'PROCESSING',
      stats: {
        totalComments: 1842,
        analyzed: 1240,
        skipped: 0,
        positive: 700,
        neutral: 340,
        negative: 200,
        positivePercent: 57,
        neutralPercent: 27,
        negativePercent: 16,
      },
      dominantSentiment: null,
    });
    const failed = sentimentFixture({
      status: 'FAILED',
      stats: {
        totalComments: 2400,
        analyzed: 0,
        skipped: 0,
        positive: 0,
        neutral: 0,
        negative: 0,
        positivePercent: 0,
        neutralPercent: 0,
        negativePercent: 0,
      },
      dominantSentiment: null,
    });
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: processing },
      { kind: 'success', data: failed },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    // Real progress from backend counts (only rendered after the backend
    // response lands): 1842 - 1240 = 602 remaining.
    await waitFor(() => {
      expect(
        screen.getByText('1,842 collected · 1,240 analyzed · 602 remaining'),
      ).toBeTruthy();
    });
    expect(screen.getByText('ANALYZING AUDIENCE')).toBeTruthy();
    // One truthful status: no "READY FOR AI ANALYSIS" beside analysis copy.
    expect(screen.getByText('Analysis running')).toBeTruthy();
    expect(screen.queryByText('READY FOR AI ANALYSIS')).toBeNull();
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();

    // FAILED -> "Analysis failed" (still no fabricated results).
    fireEvent.click(screen.getByRole('button', { name: /refresh analysis/i }));
    await waitFor(() => {
      expect(screen.getByText('Analysis failed')).toBeTruthy();
    });
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
  });

  it('shows a single truthful status after analysis completes', async () => {
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: sentimentFixture() },
    ]);
    setupWith(acquisition, sentiment);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    // Status row matches the analysis state instead of the old static copy.
    expect(screen.getByText('Analysis complete')).toBeTruthy();
    expect(screen.queryByText('READY FOR AI ANALYSIS')).toBeNull();
  });
});

describe('Sprint 4.3 - background analysis job', () => {
  /** TEST FIXTURE: backend-shaped job snapshot (never real data). */
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

  /**
   * Deferred job transport: the test drives start results, polled
   * snapshots (emit) and the terminal wait result independently - exactly
   * the sequence the real HttpJobService would produce.
   */
  class DeferredJobService implements AnalysisJobService {
    public startCalls: JobStartRequest[] = [];
    public waitCalls: JobWaitRequest[] = [];
    public aborted = false;
    private startResult: JobStartResult = {
      kind: 'started',
      job: { jobId: 'job-1', videoId: 'dQw4w9WgXcQ', status: 'QUEUED' },
    };
    private pending!: (result: JobWaitResult) => void;

    setStart(result: JobStartResult): void {
      this.startResult = result;
    }

    start(request: JobStartRequest): Promise<JobStartResult> {
      this.startCalls.push(request);
      return Promise.resolve(this.startResult);
    }

    waitForTerminal(request: JobWaitRequest): Promise<JobWaitResult> {
      this.waitCalls.push(request);
      request.signal?.addEventListener('abort', () => {
        this.aborted = true;
      });
      return new Promise<JobWaitResult>((resolve) => {
        this.pending = resolve;
      });
    }

    /** Deliver one polled snapshot exactly like the real poller would. */
    emit(job: AnalysisJob): void {
      this.waitCalls[this.waitCalls.length - 1]?.onJob?.(job);
    }

    resolve(result: JobWaitResult): void {
      this.pending(result);
    }
  }

  it('starts a job, renders REAL progress, then completes', async () => {
    const job = new DeferredJobService();
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    // Sprint 5.1 §8: TWO aggregate reads are expected on the happy path -
    // (1) the in-run progressive snapshot while the job is non-terminal,
    // (2) the final read after completion.
    const sentiment = new ScriptedSentimentService([
      { kind: 'success', data: sentimentFixture() },
      { kind: 'success', data: sentimentFixture() },
    ]);
    setupWith(acquisition, sentiment, job);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => expect(job.startCalls).toHaveLength(1));
    expect(job.startCalls[0].videoId).toBe('dQw4w9WgXcQ');

    // §25 QUEUED: preparing copy, no fabricated counts.
    await waitFor(() => {
      expect(screen.getByText('Preparing analysis…')).toBeTruthy();
    });
    expect(screen.queryByText(/comments collected/)).toBeNull();

    // §25 ACQUIRING: real collected count from the polled snapshot.
    act(() => {
      job.emit(
        jobFixture({ status: 'ACQUIRING', phase: 'ACQUISITION', collected: 1842, stored: 1842, analyzable: 1842 }),
      );
    });
    expect(screen.getByText(/collecting audience responses/i)).toBeTruthy();
    expect(screen.getByText('1,842 comments collected')).toBeTruthy();
    // Never a fabricated percentage while acquiring (§25/§33).
    expect(screen.queryByText(/%$/)).toBeNull();

    // §25 ANALYZING: count-based batch progress (no fake percentage).
    act(() => {
      job.emit(
        jobFixture({
          status: 'ANALYZING',
          phase: 'SENTIMENT',
          collected: 1842,
          stored: 1842,
          analyzable: 1842,
          analyzed: 1200,
          pending: 642,
        }),
      );
    });
    expect(screen.getByText('ANALYZING AUDIENCE')).toBeTruthy();
    expect(screen.getByText('ANALYZING')).toBeTruthy(); // status chip
    expect(
      screen.getByText('1,842 collected · 1,200 analyzed · 642 remaining'),
    ).toBeTruthy();
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();

    // Terminal COMPLETED -> two fast reads, then the final dashboard.
    const terminal = jobFixture({
      status: 'COMPLETED',
      phase: 'COMPLETE',
      collected: 1842,
      stored: 1842,
      analyzable: 1842,
      analyzed: 1842,
      pending: 0,
    });
    await act(async () => {
      job.resolve({ kind: 'completed', job: terminal });
    });
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    expect(acquisition.calls).toHaveLength(1);
    expect(acquisition.calls[0].videoId).toBe('dQw4w9WgXcQ');
    // (1) in-run progressive aggregate (§8), (2) final read after COMPLETED.
    expect(sentiment.calls).toHaveLength(2);
    expect(screen.getByText('Real Fixture Title')).toBeTruthy();
  });

  it('shows the first real aggregate while the job is still running (§8)', async () => {
    const job = new DeferredJobService();
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    // First response = partial in-run snapshot; second = final.
    const sentiment = new ScriptedSentimentService([
      {
        kind: 'success',
        data: sentimentFixture({
          stats: {
            totalComments: 5000,
            analyzed: 1024,
            skipped: 0,
            positive: 717,
            neutral: 205,
            negative: 102,
            positivePercent: 70,
            neutralPercent: 20,
            negativePercent: 10,
          },
        }),
      },
      { kind: 'success', data: sentimentFixture() },
    ]);
    setupWith(acquisition, sentiment, job);

    await waitFor(() => expect(job.startCalls).toHaveLength(1));

    // In-run snapshot: analyzed > 0 while the job is NOT terminal - the
    // backend now reports partial aggregates during the run (§8), and the
    // overlay must show them behind an explicit in-progress state.
    act(() => {
      job.emit(
        jobFixture({
          status: 'ANALYZING',
          phase: 'SENTIMENT',
          collected: 5000,
          stored: 5000,
          analyzable: 5000,
          analyzed: 1024,
          pending: 3976,
        }),
      );
    });

    // The FIRST INSIGHT view appears with real backend percentages.
    await waitFor(() => {
      expect(screen.getByText('ANALYZING…')).toBeTruthy();
    });
    expect(screen.getByText('70%')).toBeTruthy();
    expect(screen.getByText('20%')).toBeTruthy();
    expect(screen.getByText('10%')).toBeTruthy();
    expect(screen.getByText(/1,024 \/ 5,000 analyzed/)).toBeTruthy();
    // Interim is NEVER presented as final (§8): no completion badge.
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
    // Progressive aggregates are fetched once per growth tick (throttled,
    // aggregate-only - §20), plus the final read after completion.
    expect(sentiment.calls).toHaveLength(1);

    // Terminal COMPLETED: the interim view flips to the final dashboard.
    await act(async () => {
      job.resolve({
        kind: 'completed',
        job: jobFixture({
          status: 'COMPLETED',
          phase: 'COMPLETE',
          collected: 5000,
          stored: 5000,
          analyzable: 5000,
          analyzed: 5000,
          pending: 0,
        }),
      });
    });
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    });
    expect(screen.queryByText('ANALYZING…')).toBeNull();
    expect(sentiment.calls).toHaveLength(2);
  });

  it('renders the interrupted job state with real counts and retries', async () => {
    const job = new DeferredJobService();
    const acquisition = new ScriptedService([]);
    setupWith(acquisition, undefined, job);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => expect(job.startCalls).toHaveLength(1));

    const failed = jobFixture({
      status: 'FAILED',
      phase: 'ACQUISITION',
      collected: 1842,
      stored: 1842,
      errorCode: 'upstream_timeout',
      errorMessage: 'The request timed out. Please try again.',
    });
    act(() => {
      job.emit(failed); // last polled snapshot (real counts)
    });
    await act(async () => {
      job.resolve({ kind: 'failed', job: failed });
    });

    // §25 FAILED: honest interrupted copy + collected count + retry.
    await waitFor(() => {
      expect(screen.getByText('ANALYSIS INTERRUPTED')).toBeTruthy();
    });
    expect(screen.getByText(/request timed out/i)).toBeTruthy();
    expect(screen.getByText('1,842 comments collected')).toBeTruthy();
    expect(screen.getByText('ERROR')).toBeTruthy();
    // No dataset was fetched after the failed job.
    expect(acquisition.calls).toHaveLength(0);

    // §23: Retry (error-state only) restarts the automatic pipeline by
    // itself - there is no Analyze button involved.
    fireEvent.click(screen.getByRole('button', { name: /^retry$/i }));
    await waitFor(() => expect(job.startCalls).toHaveLength(2));
    expect(job.startCalls[1].videoId).toBe('dQw4w9WgXcQ');
    expect(acquisition.calls).toHaveLength(0); // still gated on the job
  });

  it('maps a job start failure to the categorized error view', async () => {
    const job = new DeferredJobService();
    job.setStart({
      kind: 'error',
      code: 'quota_exceeded',
      message: 'The YouTube API quota is exhausted for now. Please try again later.',
    });
    setupWith(new ScriptedService([]), undefined, job);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => {
      expect(screen.getByText('QUOTA EXCEEDED')).toBeTruthy();
    });
    expect(screen.getByText(/quota is exhausted for now/i)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /^retry$/i }));
    // Retry re-runs the automatic pipeline; when it fails again the same
    // categorized view returns (bounded recovery, never a silent loop).
    await waitFor(() => expect(job.startCalls).toHaveLength(2));
    await waitFor(() => {
      expect(screen.getByText('QUOTA EXCEEDED')).toBeTruthy();
    });
  });

  it('aborts polling on video switch and discards A\'s late result (§27)', async () => {
    const job = new DeferredJobService();
    const acquisition = new ScriptedService([]);
    const { store } = setupWith(acquisition, undefined, job);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => expect(job.waitCalls).toHaveLength(1));
    expect(job.waitCalls[0].videoId).toBe('dQw4w9WgXcQ');

    // Navigate to video B while A's job is still polling.
    act(() => {
      store.setVideoContext(
        parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
      );
    });
    expect(job.aborted).toBe(true); // §26: polling stops on switch
    expect(store.getState().status).toBe('loading'); // B's automatic run

    // A's terminal result lands LATE - it must change nothing on B.
    await act(async () => {
      job.resolve({
        kind: 'completed',
        job: jobFixture({ status: 'COMPLETED', phase: 'COMPLETE', collected: 1842 }),
      });
    });
    expect(store.getState().status).not.toBe('complete'); // A's completion dropped
    expect(store.getState().job.videoId).toBe('BBBBBBBBBBB'); // B owns the slice
    expect(store.getState().acquisition.data).toBeNull();
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
    expect(screen.queryByText('1,842 comments collected')).toBeNull();
    expect(acquisition.calls).toHaveLength(0); // A's fetch never ran for B

    // B's own job started automatically with B's id (§17 - no click).
    await waitFor(() => expect(job.startCalls).toHaveLength(2));
    expect(job.startCalls[1].videoId).toBe('BBBBBBBBBBB');
    expect(job.waitCalls[1].videoId).toBe('BBBBBBBBBBB');
  });

  it('job completion keeps the video-switch stale guard on the final fetch', async () => {
    // Regression guard: switching DURING the terminal wait must never let
    // the post-completion data fetch run for the new video.
    const job = new DeferredJobService();
    const acquisition = new ScriptedService([
      { kind: 'success', data: acquiredFixture() },
    ]);
    const { store } = setupWith(acquisition, undefined, job);

    // Sprint 4.4 §3/§4: analysis starts automatically when the overlay
    // opens for the detected video - there is no Analyze button to click.
    await waitFor(() => expect(job.waitCalls).toHaveLength(1));

    act(() => {
      store.setVideoContext(
        parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
      );
    });
    await act(async () => {
      job.resolve({
        kind: 'completed',
        job: jobFixture({ status: 'COMPLETED', phase: 'COMPLETE' }),
      });
    });
    // A's post-completion fetch never ran; B's automatic run is in flight.
    expect(acquisition.calls).toHaveLength(0);
    expect(store.getState().status).toBe('loading'); // B in flight, never 'complete'
    expect(store.getState().job.videoId).toBe('BBBBBBBBBBB');
  });
});
