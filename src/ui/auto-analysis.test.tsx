// @vitest-environment jsdom
/**
 * Sprint 4.4 §38-§41: zero-click automatic analysis and backend
 * availability - the video ID is the ONLY trigger.
 *
 * - Video detection (boot + SPA navigation) starts analysis with no clicks
 * - Duplicate navigation events for one video -> exactly one job (§7/§39)
 * - Rapid A -> B -> C -> D -> only D ever controls the overlay (§18/§41)
 * - CONNECTING state while the availability gate runs (§15/§19)
 * - Unavailable backend -> friendly FAILED + Retry recovery (§30/§40)
 * - No Analyze / Analyze-again / Run-analysis control exists (§3)
 */
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  AnalysisService,
  AnalysisServiceRequest,
  AnalysisServiceResult,
} from '../services/analysis/analysis-service';
import type {
  AnalysisJobService,
  JobStartRequest,
  JobStartResult,
  JobWaitRequest,
  JobWaitResult,
} from '../services/analysis/analysis-job-service';
import type {
  SentimentService,
  SentimentServiceRequest,
  SentimentServiceResult,
} from '../services/analysis/sentiment-service';
import type { BackendGate, BackendGateResult } from '../services/backend/backend-gate';
import { parseVideoContext } from '../services/youtube/video-context';
import type { SentimentAnalysis, VideoDataResponse } from '../shared/types';
import { createStore, type Store } from '../state/store';
import { App } from './App';

afterEach(() => {
  cleanup();
});

const VIDEO_A_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
const VIDEO_A = 'dQw4w9WgXcQ';

function dataFor(videoId: string, title: string): VideoDataResponse {
  return {
    video: {
      videoId,
      title,
      description: null,
      channelId: 'UC1',
      channelTitle: 'Fixture Channel',
      publishedAt: null,
      categoryId: null,
      duration: null,
      statistics: { viewCount: null, likeCount: null, commentCount: null },
    },
    comments: { items: [], count: 100, hasMore: false, status: 'ok' },
    source: { provider: 'youtube', retrievedAt: '2026-09-27T00:00:00Z', cached: false },
  };
}

function sentimentFor(videoId: string): SentimentAnalysis {
  return {
    videoId,
    status: 'PROCESSED',
    stats: {
      totalComments: 100,
      analyzed: 100,
      skipped: 0,
      positive: 50,
      neutral: 30,
      negative: 20,
      positivePercent: 50,
      neutralPercent: 30,
      negativePercent: 20,
    },
    dataset: {
      collected: 100,
      stored: 100,
      analyzed: 100,
      skipped: 0,
      failed: 0,
      hasMore: false,
      limitReached: false,
    },
    dominantSentiment: 'POSITIVE',
  };
}

/** Sentiment transport that always answers with a backend-shaped verdict. */
class AutoSentiment implements SentimentService {
  public calls: string[] = [];
  getSentiment(request: SentimentServiceRequest): Promise<SentimentServiceResult> {
    this.calls.push(request.videoId);
    return Promise.resolve({ kind: 'success', data: sentimentFor(request.videoId) });
  }
}

/** Per-call resolvers: tests decide when each response lands (races, §41). */
class TrackedAnalysis implements AnalysisService {
  public calls: AnalysisServiceRequest[] = [];
  public resolvers: Array<(result: AnalysisServiceResult) => void> = [];
  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    this.calls.push(request);
    return new Promise<AnalysisServiceResult>((resolve) => {
      this.resolvers.push(resolve);
    });
  }
}

/** Queued scripted results (happy-path fixtures). */
class ScriptedAnalysis implements AnalysisService {
  public calls: AnalysisServiceRequest[] = [];
  private readonly results: AnalysisServiceResult[];
  constructor(results: AnalysisServiceResult[]) {
    this.results = results;
  }
  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    this.calls.push(request);
    const next = this.results.shift() ?? {
      kind: 'unavailable' as const,
      reason: 'service_not_configured' as const,
    };
    return Promise.resolve(next);
  }
}

/** Background-job transport whose runs never finish on their own. */
class EndlessJobService implements AnalysisJobService {
  public startCalls: JobStartRequest[] = [];
  public waitCalls: JobWaitRequest[] = [];
  start(request: JobStartRequest): Promise<JobStartResult> {
    this.startCalls.push(request);
    return Promise.resolve({
      kind: 'started',
      job: {
        jobId: `job-${this.startCalls.length}`,
        videoId: request.videoId,
        status: 'QUEUED',
      },
    });
  }
  waitForTerminal(request: JobWaitRequest): Promise<JobWaitResult> {
    this.waitCalls.push(request);
    return new Promise<JobWaitResult>(() => {
      // Never resolves: the run stays active until the test ends.
    });
  }
}

class ScriptedGate implements BackendGate {
  public calls = 0;
  private readonly results: BackendGateResult[];
  constructor(results: BackendGateResult[]) {
    this.results = results;
  }
  ensure(): Promise<BackendGateResult> {
    this.calls += 1;
    const next = this.results.length > 1 ? this.results.shift()! : this.results[0];
    return Promise.resolve(next);
  }
}

class DeferredGate implements BackendGate {
  public calls = 0;
  private pending!: (result: BackendGateResult) => void;
  ensure(): Promise<BackendGateResult> {
    this.calls += 1;
    return new Promise<BackendGateResult>((resolve) => {
      this.pending = resolve;
    });
  }
  resolve(result: BackendGateResult): void {
    this.pending(result);
  }
}

interface Harness {
  store: Store;
  analysis: AnalysisService;
  sentiment: AutoSentiment;
}

/** Renders the app closed (FAB only) with the given transports. */
function renderClosed(
  analysis: AnalysisService,
  options: { gate?: BackendGate; jobService?: AnalysisJobService } = {},
): Harness {
  const store = createStore();
  const sentiment = new AutoSentiment();
  render(
    <App
      store={store}
      analysisService={analysis}
      sentimentService={sentiment}
      jobService={options.jobService}
      backendGate={options.gate}
    />,
  );
  return { store, analysis, sentiment };
}

function navigate(store: Store, url: string): void {
  act(() => {
    store.setVideoContext(parseVideoContext(url));
  });
}

function assertNoManualAnalyzeControls(): void {
  expect(screen.queryByRole('button', { name: /^analyze$/i })).toBeNull();
  expect(screen.queryByRole('button', { name: /analyze again/i })).toBeNull();
  expect(screen.queryByRole('button', { name: /run analysis/i })).toBeNull();
}

describe('zero-click automatic analysis (§4/§39)', () => {
  it('starts analysis from a navigation event alone - zero clicks', async () => {
    const analysis = new TrackedAnalysis();
    const { store } = renderClosed(analysis);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(analysis.calls).toHaveLength(0);

    // The only "user action" is YouTube navigating to a video (§4).
    navigate(store, VIDEO_A_URL);

    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    await waitFor(() => expect(analysis.calls).toHaveLength(1));
    expect(analysis.calls[0].videoId).toBe(VIDEO_A);
    expect(store.getState().acquisition.status).toBe('loading');
    assertNoManualAnalyzeControls();

    // Results appear automatically once the run finishes (§22).
    await act(async () => {
      analysis.resolvers[0]({ kind: 'success', data: dataFor(VIDEO_A, 'Video A Title') });
    });
    await waitFor(() => expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy());
    expect(screen.getByText('Video A Title')).toBeTruthy();
    assertNoManualAnalyzeControls();
  });

  it('duplicate navigation events for one video start exactly ONE job (§7/§39)', async () => {
    const job = new EndlessJobService();
    const analysis = new ScriptedAnalysis([]);
    const { store } = renderClosed(analysis, { jobService: job });

    navigate(store, VIDEO_A_URL);
    await waitFor(() => expect(job.startCalls).toHaveLength(1));

    // YouTube re-emits the same video repeatedly (SPA/DOM noise) - §24: no
    // new job, no dataset reset, no duplicate API traffic.
    for (let i = 0; i < 5; i += 1) {
      navigate(store, VIDEO_A_URL);
      navigate(store, `${VIDEO_A_URL}&list=RDdQw4w9WgXcQ&index=2`);
    }

    expect(job.startCalls).toHaveLength(1);
    expect(job.waitCalls).toHaveLength(1);
    expect(store.getState().job.jobId).toBe('job-1');
    expect(store.getState().status).toBe('loading');
    expect(store.getState().videoContext?.videoId).toBe(VIDEO_A);
    assertNoManualAnalyzeControls();
  });

  it('boot detection on a watch page auto-opens and starts without any click', async () => {
    const analysis = new TrackedAnalysis();
    const sentiment = new AutoSentiment();
    // Production boot order (content/index.ts): store created -> detector
    // reports the current URL -> React mounts on the already-detected video.
    const store = createStore();
    store.setVideoContext(parseVideoContext(VIDEO_A_URL));
    render(
      <App
        store={store}
        analysisService={analysis}
        sentimentService={sentiment}
      />,
    );

    // Fresh session on a watch page: overlay is open, run in progress,
    // even though the test never clicked anything (§29 direct URL / refresh).
    expect(screen.getByRole('dialog', { name: /ai analyzer overlay/i })).toBeTruthy();
    await waitFor(() => expect(analysis.calls).toHaveLength(1));
    expect(analysis.calls[0].videoId).toBe(VIDEO_A);
    assertNoManualAnalyzeControls();
  });
});

describe('rapid video switching (§18/§41)', () => {
  it('A -> B -> C -> D: only D controls the overlay; late results are dropped', async () => {
    const analysis = new TrackedAnalysis();
    const { store } = renderClosed(analysis);

    navigate(store, 'https://www.youtube.com/watch?v=AAAAAAAAAAA');
    navigate(store, 'https://www.youtube.com/watch?v=BBBBBBBBBBB');
    navigate(store, 'https://www.youtube.com/watch?v=CCCCCCCCCCC');
    navigate(store, 'https://www.youtube.com/watch?v=DDDDDDDDDDD');

    // One run per video, in order; D is the active one.
    await waitFor(() => expect(analysis.calls).toHaveLength(4));
    expect(analysis.calls.map((call) => call.videoId)).toEqual([
      'AAAAAAAAAAA',
      'BBBBBBBBBBB',
      'CCCCCCCCCCC',
      'DDDDDDDDDDD',
    ]);
    expect(store.getState().videoContext?.videoId).toBe('DDDDDDDDDDD');

    // D completes first and drives the overlay.
    await act(async () => {
      analysis.resolvers[3]({
        kind: 'success',
        data: dataFor('DDDDDDDDDDD', 'Video D Title'),
      });
    });
    await waitFor(() => expect(screen.getByText('Video D Title')).toBeTruthy());
    expect(store.getState().status).toBe('complete');

    // Every superseded response lands afterwards - none may overwrite D.
    await act(async () => {
      analysis.resolvers[0]({ kind: 'success', data: dataFor('AAAAAAAAAAA', 'Video A Title') });
      analysis.resolvers[1]({ kind: 'success', data: dataFor('BBBBBBBBBBB', 'Video B Title') });
      analysis.resolvers[2]({ kind: 'success', data: dataFor('CCCCCCCCCCC', 'Video C Title') });
    });

    expect(screen.getByText('Video D Title')).toBeTruthy();
    expect(screen.queryByText('Video A Title')).toBeNull();
    expect(screen.queryByText('Video B Title')).toBeNull();
    expect(screen.queryByText('Video C Title')).toBeNull();
    expect(store.getState().acquisition.videoId).toBe('DDDDDDDDDDD');
    expect(store.getState().videoContext?.videoId).toBe('DDDDDDDDDDD');
    expect(store.getState().status).toBe('complete');
    assertNoManualAnalyzeControls();
  });
});

describe('backend availability (§15/§30/§40)', () => {
  it('shows CONNECTING while the gate runs, then starts analysis on ready', async () => {
    const gate = new DeferredGate();
    const analysis = new TrackedAnalysis();
    const { store } = renderClosed(analysis, { gate });

    navigate(store, VIDEO_A_URL);

    // Overlay is open and honest about the gate - never frozen (§19).
    expect(store.getState().status).toBe('connecting');
    // Header chip + body view both say CONNECTING.
    expect(screen.getAllByText('CONNECTING').length).toBeGreaterThan(0);
    expect(screen.getByText(/starting the local ai service/i)).toBeTruthy();
    expect(analysis.calls).toHaveLength(0); // nothing starts before ready

    await act(async () => {
      gate.resolve({ kind: 'ready', outcome: 'started' });
    });
    await waitFor(() => expect(analysis.calls).toHaveLength(1));
    expect(analysis.calls[0].videoId).toBe(VIDEO_A);
    await waitFor(() => expect(store.getState().status).toBe('loading'));
    expect(gate.calls).toBe(1);
  });

  it('startup failure shows a friendly FAILED state; Retry recovers (§30/§40)', async () => {
    const gate = new ScriptedGate([
      { kind: 'unavailable', reason: 'native_failed' },
      { kind: 'ready', outcome: 'started' },
    ]);
    const analysis = new ScriptedAnalysis([
      { kind: 'success', data: dataFor(VIDEO_A, 'Video A Title') },
    ]);
    const { store } = renderClosed(analysis, { gate });

    navigate(store, VIDEO_A_URL);
    await waitFor(() => {
      expect(screen.getByText('BACKEND UNAVAILABLE')).toBeTruthy();
    });
    // Friendly copy - never connection/stack/endpoint details (§30/§32).
    expect(screen.getByText(/local ai service is unavailable/i)).toBeTruthy();
    expect(
      screen.queryByText(/127\.0\.0\.1|:8000|connection refused|traceback|fetch/i),
    ).toBeNull();
    expect(store.getState().status).toBe('error');
    // Nothing was attempted while the backend was down (§15 ordering).
    expect(analysis.calls).toHaveLength(0);
    // The ONE allowed manual control in a failure state (§23).
    const retry = screen.getByRole('button', { name: /^retry$/i });
    fireEvent.click(retry);

    await waitFor(() => expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy());
    expect(gate.calls).toBe(2); // bounded recovery, not an automatic loop
    expect(analysis.calls).toHaveLength(1);
    expect(screen.getByText('Video A Title')).toBeTruthy();
    assertNoManualAnalyzeControls();
  });

  it('an already-running backend goes straight from CONNECTING to analysis', async () => {
    const gate = new ScriptedGate([{ kind: 'ready', outcome: 'already_running' }]);
    const analysis = new TrackedAnalysis();
    const { store } = renderClosed(analysis, { gate });
    navigate(store, VIDEO_A_URL);

    // The gate resolves immediately, so analysis starts right away.
    await waitFor(() => expect(analysis.calls).toHaveLength(1));
    expect(analysis.calls[0].videoId).toBe(VIDEO_A);
    expect(gate.calls).toBe(1);
    assertNoManualAnalyzeControls();
  });
});
