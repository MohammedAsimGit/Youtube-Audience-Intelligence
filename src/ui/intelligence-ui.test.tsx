// @vitest-environment jsdom
/**
 * Sprint 5.2 §31 - interaction and rendering tests for the redesigned
 * intelligence UI (hero, tooltips, progressive disclosure, bar mapping,
 * coverage counters, FAB intelligence states).
 *
 * Every value below is backend-shaped: the UI must render exactly what the
 * body says (§27) and never invent copy or percentages.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  AnalysisService,
  AnalysisServiceRequest,
  AnalysisServiceResult,
} from '../services/analysis/analysis-service';
import type {
  SentimentService,
  SentimentServiceRequest,
  SentimentServiceResult,
} from '../services/analysis/sentiment-service';
import { parseVideoContext } from '../services/youtube/video-context';
import type { SentimentAnalysis, VideoDataResponse } from '../shared/types';
import { createStore } from '../state/store';
import { App } from './App';

afterEach(() => {
  cleanup();
});

const VIDEO_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
const VIDEO = 'dQw4w9WgXcQ';

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
    comments: { items: [], count: 10, hasMore: false, status: 'ok' },
    source: { provider: 'youtube', retrievedAt: '2026-09-27T00:00:00Z', cached: false },
  };
}

function baseBody(videoId: string): SentimentAnalysis {
  return {
    videoId,
    status: 'PROCESSED',
    stats: {
      totalComments: 10,
      analyzed: 10,
      skipped: 0,
      positive: 6,
      neutral: 2,
      negative: 2,
      positivePercent: 60,
      neutralPercent: 20,
      negativePercent: 20,
    },
    dataset: {
      collected: 10,
      stored: 10,
      analyzed: 10,
      skipped: 0,
      failed: 0,
      hasMore: false,
      limitReached: false,
    },
    dominantSentiment: 'POSITIVE',
  };
}

/** Full intelligence body: emotion + intensity + confidence + mood. */
function intelligenceBody(videoId: string): SentimentAnalysis {
  return {
    ...baseBody(videoId),
    emotion: {
      dominant: 'JOY',
      dominantPercent: 40,
      distribution: {
        FEAR: { count: 1, percent: 10 },
        ANGER: { count: 0, percent: 0 },
        ANTICIPATION: { count: 1, percent: 10 },
        TRUST: { count: 0, percent: 0 },
        SURPRISE: { count: 0, percent: 0 },
        SADNESS: { count: 1, percent: 10 },
        DISGUST: { count: 0, percent: 0 },
        JOY: { count: 4, percent: 40 },
        NEUTRAL: { count: 3, percent: 30 },
      },
    },
    intensity: {
      overall: 'MEDIUM',
      distribution: {
        LOW: { count: 3, percent: 30 },
        MEDIUM: { count: 5, percent: 50 },
        HIGH: { count: 2, percent: 20 },
      },
    },
    confidence: { average: 0.84 },
    audienceMood: 'EXCITED',
  };
}

/** Analysis transport that completes immediately (zero-click pipeline). */
class ImmediateAnalysis implements AnalysisService {
  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    return Promise.resolve({ kind: 'success', data: dataFor(request.videoId) });
  }
}

/** Categorized failure transport (§30 retry-exists-only-for-failures). */
class FailingAnalysis implements AnalysisService {
  analyze(): Promise<AnalysisServiceResult> {
    return Promise.resolve({
      kind: 'error',
      code: 'network_error',
      message: 'The local AI service could not be reached.',
    });
  }
}

/** Sentiment transport returning the given backend body as-is. */
class ScriptedSentiment implements SentimentService {
  constructor(private readonly body: SentimentAnalysis) {}
  getSentiment(_request: SentimentServiceRequest): Promise<SentimentServiceResult> {
    return Promise.resolve({ kind: 'success', data: this.body });
  }
}

/** Renders the app on the video and waits for the requested milestone. */
async function renderResults(
  body: SentimentAnalysis,
  analysis: AnalysisService = new ImmediateAnalysis(),
  waitText = 'ANALYSIS COMPLETE',
): Promise<void> {
  const store = createStore();
  render(
    <App
      store={store}
      analysisService={analysis}
      sentimentService={new ScriptedSentiment(body)}
    />,
  );
  act(() => {
    store.setVideoContext(parseVideoContext(VIDEO_URL));
  });
  await waitFor(() => expect(screen.getByText(waitText)).toBeTruthy());
}

describe('Sprint 5.2 - hero (L1 instant understanding)', () => {
  it('states the majority verdict with the real share and response count', async () => {
    await renderResults(baseBody(VIDEO));
    const hero = within(screen.getByRole('region', { name: 'Overall audience reaction' }));
    expect(hero.getByText('Mostly Positive')).toBeTruthy();
    expect(hero.getByText('60%')).toBeTruthy();
    expect(hero.getByText('10 audience responses')).toBeTruthy();
    expect(hero.getByText('Intelligence Active')).toBeTruthy();
    // Screen readers get the same truthful values (ring is aria-hidden).
    expect(hero.getByText(/POSITIVE: 60 percent/)).toBeTruthy();
  });

  it('never claims a majority when the dominant share is below 50%', async () => {
    const body = baseBody(VIDEO);
    body.stats = {
      ...body.stats,
      positive: 4,
      neutral: 3,
      negative: 3,
      positivePercent: 40,
      neutralPercent: 30,
      negativePercent: 30,
    };
    await renderResults(body);
    expect(screen.getByText('Positive Leads')).toBeTruthy();
    expect(screen.queryByText('Mostly Positive')).toBeNull();
  });
});

describe('Sprint 5.2 - progressive disclosure and tooltips', () => {
  it('keeps the four technical tooltips in the DOM with accessible names', async () => {
    await renderResults(intelligenceBody(VIDEO));
    expect(
      screen.getByRole('button', { name: 'About model confidence' }),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: 'About audience mood' })).toBeTruthy();
    expect(
      screen.getByRole('button', { name: 'About emotional intensity' }),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: 'About skipped comments' })).toBeTruthy();
    // Copy itself lives in role=tooltip elements (CSS reveals on hover/focus).
    expect(
      screen.getByText(/how clearly the model distinguished/i),
    ).toBeTruthy();
    expect(screen.getByText(/derived from analyzed sentiment/i)).toBeTruthy();
    expect(screen.getByText(/not currently supported/i)).toBeTruthy();
  });

  it('hides model terminology until Technical Information is expanded', async () => {
    await renderResults(intelligenceBody(VIDEO));
    expect(screen.queryByText(/NRC emotion model/i)).toBeNull();
    expect(screen.queryByText(VIDEO)).toBeNull();
    const toggle = screen.getByRole('button', { name: /technical information/i });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText(/VADER sentiment · NRC emotion model/i)).toBeTruthy();
    expect(screen.getByText(VIDEO)).toBeTruthy();
    expect(screen.getByText('YouTube')).toBeTruthy();
  });

  it('reveals the honest "Why this emotion?" explanation on demand', async () => {
    await renderResults(intelligenceBody(VIDEO));
    expect(
      screen.queryByText(/most frequently detected emotion/i),
    ).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /why this emotion/i }));
    expect(
      screen.getByText(/Joy was the most frequently detected emotion/i),
    ).toBeTruthy();
  });
});

describe('Sprint 5.2 - data mapping and coverage', () => {
  it('maps backend percentages straight onto the distribution bar widths', async () => {
    await renderResults(baseBody(VIDEO));
    const distribution = within(
      screen.getByRole('region', { name: 'Sentiment distribution' }),
    );
    expect(distribution.getByText('60%')).toBeTruthy();
    expect(distribution.getByText('6')).toBeTruthy(); // 6 of 10 analyzed
    const positiveFill = document.querySelector('.sai-bar-fill[data-kind="positive"]');
    const neutralFill = document.querySelector('.sai-bar-fill[data-kind="neutral"]');
    expect(positiveFill?.getAttribute('style')).toContain('width: 60%');
    expect(neutralFill?.getAttribute('style')).toContain('width: 20%');
  });

  it('shows a failed counter only when the backend reports failures', async () => {
    await renderResults(baseBody(VIDEO));
    const coverage = within(screen.getByRole('region', { name: 'Analysis coverage' }));
    // Collected and analyzed are both 10 on this fixture - two counters.
    expect(coverage.getAllByText('10').length).toBe(2);
    expect(coverage.getByText('comments collected')).toBeTruthy();
    expect(coverage.getByText('analyzed')).toBeTruthy();
    expect(coverage.queryByText('failed')).toBeNull();
    cleanup();

    const failedBody = baseBody(VIDEO);
    failedBody.dataset = { ...failedBody.dataset, failed: 2, analyzed: 8 };
    failedBody.stats = { ...failedBody.stats, analyzed: 8, skipped: 0 };
    await renderResults(failedBody);
    const coverage2 = within(screen.getByRole('region', { name: 'Analysis coverage' }));
    expect(coverage2.getByText('2')).toBeTruthy();
    expect(coverage2.getByText('failed')).toBeTruthy();
    expect(coverage2.getByText(/Based on 8 analyzed comments/)).toBeTruthy();
  });
});

describe('Sprint 5.2 - FAB intelligence states (§23)', () => {
  async function closeOverlay(): Promise<HTMLElement> {
    fireEvent.click(screen.getByRole('button', { name: 'Close AI Analyzer' }));
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /ai analyzer overlay/i })).toBeNull(),
    );
    return screen.getByRole('button', { name: 'Open AI Analyzer' });
  }

  it('reports complete after a successful run', async () => {
    await renderResults(baseBody(VIDEO));
    const fab = await closeOverlay();
    expect(fab.getAttribute('data-state')).toBe('complete');
  });

  it('reports analyzing while the backend is still processing', async () => {
    const body = baseBody(VIDEO);
    body.status = 'PROCESSING';
    body.dataset = { ...body.dataset, analyzed: 4 };
    body.stats = { ...body.stats, analyzed: 4 };
    await renderResults(body, new ImmediateAnalysis(), 'Analysis running');
    const fab = await closeOverlay();
    expect(fab.getAttribute('data-state')).toBe('analyzing');
  });

  it('reports error after a categorized acquisition failure', async () => {
    await renderResults(
      baseBody(VIDEO),
      new FailingAnalysis(),
      'BACKEND UNAVAILABLE',
    );
    // Failure view is on screen (Retry exists only for genuine failures).
    expect(screen.getByRole('button', { name: /^retry$/i })).toBeTruthy();
    const fab = await closeOverlay();
    expect(fab.getAttribute('data-state')).toBe('error');
  });
});
