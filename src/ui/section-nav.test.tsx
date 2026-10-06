// @vitest-environment jsdom
/**
 * Multi-section console redesign - navigation + data-preservation tests (§32).
 *
 * Covered here:
 * - nav default/click/active/keyboard
 * - exact existing values render in their NEW section locations (scoped
 *   `within(region)` queries - stronger than the old flat queries)
 * - switching sections never refetches (data stays in memory)
 * - realtime updates never move the active section
 * - minimize → restore keeps the section; video switch resets it + clears
 *   stale section content
 * - error/loading states per section; no fabricated fallback values
 */
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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
import type {
  TopicsService,
  TopicsServiceRequest,
  TopicsServiceResult,
} from '../services/analysis/topics-service';
import { parseVideoContext } from '../services/youtube/video-context';
import type {
  InsightAnalysis as InsightBody,
  RealtimeStatus,
  SentimentAnalysis,
  TopicAnalysis as TopicsBody,
  VideoDataResponse,
} from '../shared/types';
import { createStore, type Store } from '../state/store';
import { App } from './App';

const VIDEO_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
const VIDEO_B_URL = 'https://www.youtube.com/watch?v=BBBBBBBBBBB';
const VIDEO = 'dQw4w9WgXcQ';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Fixtures (backend-shaped, synthetic - same shape as the other suites)
// ---------------------------------------------------------------------------

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
    source: { provider: 'youtube', retrievedAt: '2026-10-01T00:00:00Z', cached: false },
  };
}

/** Full Sprint 5 body: 10 analyzed, POSITIVE 60%, JOY, MEDIUM, EXCITED. */
function fullSentiment(videoId: string): SentimentAnalysis {
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

/** Pre-Sprint-5 body: sentiment only, no intelligence blocks. */
function legacySentiment(videoId: string): SentimentAnalysis {
  const body = fullSentiment(videoId);
  delete body.emotion;
  delete body.intensity;
  delete body.confidence;
  body.audienceMood = null;
  return body;
}

function realtimeBody(videoId: string): RealtimeStatus {
  return {
    videoId,
    enabled: true,
    monitoring: true,
    pollIntervalSeconds: 15,
    lastCheckedAt: '2026-10-02T10:00:00+00:00',
    lastUpdatedAt: '2026-10-02T09:59:00+00:00',
    newComments: 22,
    totalComments: 10,
    analyzed: 10,
    pending: 0,
    skipped: 0,
    failed: 0,
    sentiment: { positive: 60, neutral: 20, negative: 20 },
    trend: {
      state: 'RISING',
      changePp: 4.2,
      positivePp: 4.2,
      neutralPp: -3.1,
      negativePp: -1.1,
    },
    activity: { level: 'MODERATE', newRecent: 5, windowMinutes: 1, ratePerMinute: 3.4 },
    dominantEmotion: 'JOY',
    version: '10:10',
  };
}

function insightBody(videoId: string): InsightBody {
  return {
    videoId,
    status: 'READY',
    message: null,
    headline: 'The audience response is strongly positive',
    summary: 'Derived from analyzed audience data.',
    cards: [],
    sample: { collected: 10, analyzed: 10, skipped: 0 },
    source: 'deterministic',
    provider: { name: 'deterministic', model: null },
    evidenceVersion: '10:stamp',
    generatedAt: '2026-10-01T12:00:00Z',
    generationMs: 3,
  };
}

function topicsBody(videoId: string): TopicsBody {
  const topic = {
    topicId: 'learning-roadmap',
    label: 'Learning roadmap',
    mentions: 5,
    sharePercent: 50,
    keyPhrases: ['roadmap'],
    category: 'MOST_DISCUSSED' as const,
    confidence: 0.8,
    evidence: 'Five analyzed comments mention the learning roadmap.',
    sentiment: {
      POSITIVE: { count: 4, percent: 80 },
      NEUTRAL: { count: 1, percent: 20 },
      NEGATIVE: { count: 0, percent: 0 },
    },
    dominantSentiment: 'POSITIVE' as const,
  };
  return {
    videoId,
    status: 'READY',
    analyzedComments: 10,
    message: null,
    topics: [topic],
    mostDiscussed: [topic],
    mostAppreciated: [],
    painPoints: [],
    mixedTopics: [],
  };
}

// ---------------------------------------------------------------------------
// Scripted transports (one-shot or counting)
// ---------------------------------------------------------------------------

class ImmediateAnalysis implements AnalysisService {
  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    return Promise.resolve({ kind: 'success', data: dataFor(request.videoId) });
  }
}

class CountingSentiment implements SentimentService {
  readonly requests: SentimentServiceRequest[] = [];
  constructor(private readonly body: SentimentAnalysis) {}
  getSentiment(request: SentimentServiceRequest): Promise<SentimentServiceResult> {
    this.requests.push(request);
    return Promise.resolve({ kind: 'success', data: { ...this.body, videoId: request.videoId } });
  }
}

class FailingSentiment implements SentimentService {
  getSentiment(): Promise<SentimentServiceResult> {
    return Promise.resolve({
      kind: 'error',
      code: 'storage_unavailable',
      message: 'The dataset store is unavailable right now. Please try again later.',
    });
  }
}

class PendingSentiment implements SentimentService {
  getSentiment(): Promise<SentimentServiceResult> {
    return new Promise<SentimentServiceResult>(() => {
      /* never resolves - the analyzing window stays open */
    });
  }
}

class OneShotRealtime implements RealtimeService {
  readonly requests: RealtimeServiceRequest[] = [];
  constructor(private readonly versions: string[]) {}
  getRealtime(request: RealtimeServiceRequest): Promise<RealtimeServiceResult> {
    const version =
      this.versions[Math.min(this.requests.length, this.versions.length - 1)];
    this.requests.push(request);
    return Promise.resolve({ kind: 'success', data: { ...realtimeBody(request.videoId), version } });
  }
}

class OneShotInsight implements InsightService {
  getInsight(request: InsightServiceRequest): Promise<InsightServiceResult> {
    return Promise.resolve({ kind: 'success', data: insightBody(request.videoId) });
  }
}

class OneShotTopics implements TopicsService {
  getTopics(request: TopicsServiceRequest): Promise<TopicsServiceResult> {
    return Promise.resolve({ kind: 'success', data: topicsBody(request.videoId) });
  }
}

interface ConsoleOptions {
  sentiment?: SentimentService;
  realtime?: RealtimeService;
  insight?: InsightService;
  topics?: TopicsService;
}

/** Renders the app, navigates to the video, waits for the final analysis. */
async function renderConsole(options: ConsoleOptions = {}): Promise<{
  store: Store;
  sentiment: CountingSentiment | SentimentService;
}> {
  const sentiment =
    options.sentiment ?? new CountingSentiment(fullSentiment(VIDEO));
  const store = createStore();
  render(
    <App
      store={store}
      analysisService={new ImmediateAnalysis()}
      sentimentService={sentiment}
      realtimeService={options.realtime}
      insightService={options.insight}
      topicsService={options.topics}
    />,
  );
  act(() => {
    store.setVideoContext(parseVideoContext(VIDEO_URL));
  });
  await waitFor(() => expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy());
  return { store, sentiment };
}

const nav = () => screen.getByRole('navigation', { name: 'Intelligence sections' });
const tab = (name: string) => screen.getByRole('button', { name });
const region = (name: string) => screen.getByRole('region', { name });
const activeOf = (name: string): string | null =>
  region(name).getAttribute('data-active');

// ---------------------------------------------------------------------------
// Navigation behavior
// ---------------------------------------------------------------------------

describe('section navigation (§32: default/click/active/keyboard)', () => {
  it('renders five entries with Overview active by default', async () => {
    await renderConsole();
    const bar = nav();
    expect(within(bar).getAllByRole('button')).toHaveLength(5);
    expect(within(bar).getByRole('button', { name: 'Overview' })).toBeTruthy();
    expect(within(bar).getByRole('button', { name: 'Sentiment' })).toBeTruthy();
    expect(within(bar).getByRole('button', { name: 'Emotion' })).toBeTruthy();
    expect(within(bar).getByRole('button', { name: 'Topics' })).toBeTruthy();
    expect(within(bar).getByRole('button', { name: 'Insight' })).toBeTruthy();

    expect(tab('Overview').getAttribute('aria-current')).toBe('true');
    expect(tab('Sentiment').getAttribute('aria-current')).toBeNull();
    expect(activeOf('Overview')).toBe('true');
    expect(activeOf('Sentiment analysis')).toBe('false');
    expect(activeOf('Emotion intelligence')).toBe('false');
  });

  it('switches the active section on click (pure frontend, no reload)', async () => {
    const { store } = await renderConsole();
    fireEvent.click(tab('Sentiment'));
    expect(store.getState().activeSection).toBe('sentiment');
    expect(tab('Sentiment').getAttribute('aria-current')).toBe('true');
    expect(tab('Overview').getAttribute('aria-current')).toBeNull();
    expect(activeOf('Sentiment analysis')).toBe('true');
    expect(activeOf('Overview')).toBe('false');

    fireEvent.click(tab('Insight'));
    expect(store.getState().activeSection).toBe('insight');
    expect(activeOf('AI audience insight')).toBe('true');
    expect(activeOf('Sentiment analysis')).toBe('false');
  });

  it('supports keyboard navigation (Arrow/Home/End)', async () => {
    const { store } = await renderConsole();
    fireEvent.keyDown(tab('Overview'), { key: 'ArrowRight' });
    expect(store.getState().activeSection).toBe('sentiment');
    expect(document.activeElement).toBe(tab('Sentiment'));

    fireEvent.keyDown(tab('Sentiment'), { key: 'End' });
    expect(store.getState().activeSection).toBe('insight');

    fireEvent.keyDown(tab('Insight'), { key: 'ArrowLeft' });
    expect(store.getState().activeSection).toBe('topics');

    fireEvent.keyDown(tab('Topics'), { key: 'Home' });
    expect(store.getState().activeSection).toBe('overview');
    expect(document.activeElement).toBe(tab('Overview'));
  });

  it('quick glance cards jump to the Emotion section', async () => {
    const { store } = await renderConsole();
    fireEvent.click(screen.getByRole('button', { name: 'View emotion details' }));
    expect(store.getState().activeSection).toBe('emotion');
    expect(activeOf('Emotion intelligence')).toBe('true');
  });
});

// ---------------------------------------------------------------------------
// Data preservation in the new locations
// ---------------------------------------------------------------------------

describe('section content (§32: values stay exact, in their new homes)', () => {
  it('keeps every existing value in its new section (scoped queries)', async () => {
    await renderConsole({
      realtime: new OneShotRealtime(['10:10']),
      insight: new OneShotInsight(),
      topics: new OneShotTopics(),
    });
    // Async slices land after the base analysis - wait for the real data.
    await screen.findByText('What People Talk About');
    await screen.findByRole('region', { name: 'Realtime audience monitor' });
    await screen.findByRole('region', { name: 'AI Audience Insight' });

    // OVERVIEW: hero + video identity + glance cards + live strip.
    const overview = within(region('Overview'));
    expect(overview.getByText('Mostly Positive')).toBeTruthy();
    expect(overview.getByText('60%')).toBeTruthy();
    expect(overview.getByText('10 audience responses')).toBeTruthy();
    expect(overview.getByRole('region', { name: 'Acquired video' })).toBeTruthy();
    expect(overview.getByText('Fixture Video Title')).toBeTruthy();
    expect(overview.getByText('Analysis complete')).toBeTruthy();
    expect(overview.getByText('JOY')).toBeTruthy();
    expect(overview.getByText('EXCITED')).toBeTruthy();
    expect(overview.getByText('MEDIUM')).toBeTruthy(); // ENERGY tile
    expect(
      overview.getByRole('region', { name: 'Realtime audience monitor' }),
    ).toBeTruthy();

    // SENTIMENT: distribution + movement + coverage + quality.
    const sentiment = within(region('Sentiment analysis'));
    expect(sentiment.getByRole('region', { name: 'Sentiment distribution' })).toBeTruthy();
    expect(sentiment.getAllByText('60%').length).toBeGreaterThan(0);
    expect(sentiment.getByText('Positive')).toBeTruthy();
    expect(sentiment.getByRole('region', { name: 'Audience movement' })).toBeTruthy();
    expect(sentiment.getByText('+4.2pp')).toBeTruthy();
    const coverage = sentiment.getByRole('region', { name: 'Analysis coverage' });
    // Collected and analyzed are both 10 on this fixture - two counters.
    expect(within(coverage).getAllByText('10').length).toBe(2);
    expect(within(coverage).getByText(/Based on 10 analyzed comments/)).toBeTruthy();
    expect(sentiment.getByText('Analysis Quality')).toBeTruthy();
    expect(sentiment.getByText('84%')).toBeTruthy();
    expect(sentiment.getByText('sentiment decision margin')).toBeTruthy();

    // EMOTION: premium emotion + energy spectrum + mood.
    const emotion = within(region('Emotion intelligence'));
    expect(emotion.getByRole('region', { name: 'Audience emotion' })).toBeTruthy();
    expect(emotion.getByText('JOY')).toBeTruthy();
    expect(emotion.getByText('40%')).toBeTruthy();
    expect(emotion.getByText('of 10 analyzed comments')).toBeTruthy();
    expect(emotion.getByRole('region', { name: 'Emotional intensity' })).toBeTruthy();
    expect(emotion.getByRole('region', { name: 'Audience mood' })).toBeTruthy();

    // TOPICS: ranked discussion groups with backend counts.
    const topics = within(region('Discussion topics'));
    expect(topics.getByText('What People Talk About')).toBeTruthy();
    expect(topics.getByText('Learning roadmap')).toBeTruthy();
    expect(topics.getByText('01')).toBeTruthy();
    // This fixture has topics but NO pain points → the honest empty-state
    // line renders (never a fabricated concern), while the group itself
    // stays absent.
    expect(
      topics.getByText('No recurring negative theme reached the evidence threshold.'),
    ).toBeTruthy();
    expect(topics.queryByText('What People Struggle With')).toBeNull();

    // INSIGHT: briefing card with the honest source badge.
    const insight = within(region('AI audience insight'));
    expect(insight.getByRole('region', { name: 'AI Audience Insight' })).toBeTruthy();
    expect(insight.getByText('The audience response is strongly positive')).toBeTruthy();
    expect(insight.getByText('Data-derived')).toBeTruthy();

    // Secondary control: Technical Information stays outside the nav.
    expect(screen.queryByRole('navigation', { name: 'Intelligence sections' })).toBeTruthy();
    expect(
      screen.getByRole('button', { name: /technical information/i }),
    ).toBeTruthy();
  });

  it('never refetches when switching sections (data stays in memory)', async () => {
    const { store, sentiment } = await renderConsole();
    const counting = sentiment as CountingSentiment;
    expect(counting.requests).toHaveLength(1);

    fireEvent.click(tab('Sentiment'));
    fireEvent.click(tab('Emotion'));
    fireEvent.click(tab('Topics'));
    fireEvent.click(tab('Insight'));
    fireEvent.click(tab('Overview'));

    // Pure view switching: no transport was touched, latest data is still
    // rendered from memory.
    expect(counting.requests).toHaveLength(1);
    expect(store.getState().activeSection).toBe('overview');
    expect(within(region('Overview')).getByText('Mostly Positive')).toBeTruthy();
  });

  it('does not invent values for a legacy body without intelligence blocks', async () => {
    await renderConsole({ sentiment: new CountingSentiment(legacySentiment(VIDEO)) });

    // Glance tiles are data-gated exactly like their full cards.
    expect(screen.queryByRole('button', { name: 'View emotion details' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'View audience mood details' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'View emotional energy details' })).toBeNull();
    expect(screen.queryByText('JOY')).toBeNull();
    expect(screen.queryByText('EXCITED')).toBeNull();

    // Honest absence: legacy body renders no emotion value at all - no
    // invented placeholder metric, no card without its backend block.
    const emotion = within(region('Emotion intelligence'));
    expect(emotion.queryByText('Audience Emotion')).toBeNull();
    expect(emotion.queryByText('Emotional Energy')).toBeNull();
    expect(emotion.queryByText('Audience Mood')).toBeNull();
    expect(
      emotion.getByText('No emotion breakdown was provided by this analysis.'),
    ).toBeTruthy();
    // Sentiment/overview still render the real analysis.
    expect(within(region('Overview')).getByText('Mostly Positive')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Behavior guarantees across updates, minimize and video changes
// ---------------------------------------------------------------------------

describe('section state guarantees (§32: realtime/minimize/video)', () => {
  it('keeps the active section when a realtime update lands', async () => {
    vi.useFakeTimers();
    const realtime = new OneShotRealtime(['10:10', '11:11']);
    const sentiment = new CountingSentiment(fullSentiment(VIDEO));
    const store = createStore();
    render(
      <App
        store={store}
        analysisService={new ImmediateAnalysis()}
        sentimentService={sentiment}
        realtimeService={realtime}
      />,
    );
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    // The whole zero-click flow resolves on microtasks + the first tick.
    expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    expect(realtime.requests.length).toBeGreaterThanOrEqual(1);

    fireEvent.click(tab('Emotion'));
    expect(store.getState().activeSection).toBe('emotion');
    const sentimentCalls = sentiment.requests.length;

    // Version flip → silent background refetch + rerender.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(realtime.requests.length).toBeGreaterThanOrEqual(2);
    expect(sentiment.requests.length).toBeGreaterThan(sentimentCalls);
    // The user was NOT moved: section is untouched by the update.
    expect(store.getState().activeSection).toBe('emotion');
    expect(activeOf('Emotion intelligence')).toBe('true');
    expect(activeOf('Overview')).toBe('false');
    // And the fresh data still renders inside the sections.
    expect(within(region('Emotion intelligence')).getByText('JOY')).toBeTruthy();
  });

  it('preserves the active section across minimize → restore', async () => {
    const { store } = await renderConsole();
    fireEvent.click(tab('Topics'));
    expect(store.getState().activeSection).toBe('topics');

    fireEvent.click(screen.getByRole('button', { name: 'Minimize AI Analyzer panel' }));
    expect(store.getState().minimized).toBe(true);
    expect(screen.queryByRole('navigation', { name: 'Intelligence sections' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Restore AI Analyzer panel' }));
    expect(store.getState().minimized).toBe(false);
    expect(store.getState().activeSection).toBe('topics');
    expect(tab('Topics').getAttribute('aria-current')).toBe('true');
    expect(activeOf('Discussion topics')).toBe('true');
  });

  it('resets to Overview and clears stale sections on video change', async () => {
    const { store } = await renderConsole();
    fireEvent.click(tab('Emotion'));
    expect(store.getState().activeSection).toBe('emotion');

    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_B_URL));
    });
    // Section state resets with the video's data slices - no stale content.
    expect(store.getState().activeSection).toBe('overview');
    expect(screen.queryByText('Fixture Video Title')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Emotion intelligence' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Sentiment analysis' })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Per-section loading / error states (no fake fallbacks)
// ---------------------------------------------------------------------------

describe('section states (§32: loading/error per section)', () => {
  it('shows the sentiment pipeline inside the Sentiment section while loading', async () => {
    const store = createStore();
    render(
      <App
        store={store}
        analysisService={new ImmediateAnalysis()}
        sentimentService={new PendingSentiment()}
      />,
    );
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
    });
    await waitFor(() => expect(screen.getByText('Fixture Video Title')).toBeTruthy());
    await waitFor(() => expect(screen.getByText('ANALYZING AUDIENCE')).toBeTruthy());

    // Nav is present with the acquired console; the pipeline lives in its
    // section, the overview points at it - no fabricated results anywhere.
    expect(nav()).toBeTruthy();
    expect(
      within(region('Sentiment analysis')).getByText('ANALYZING AUDIENCE'),
    ).toBeTruthy();
    expect(within(region('Overview')).queryByText('ANALYZING AUDIENCE')).toBeNull();
    expect(within(region('Overview')).getByText('Analysis running')).toBeTruthy();
    expect(screen.queryByText('ANALYSIS COMPLETE')).toBeNull();
    expect(screen.queryByText('60%')).toBeNull();
  });

  it('keeps the sentiment error + Retry inside the Sentiment section', async () => {
    const store = createStore();
    render(
      <App
        store={store}
        analysisService={new ImmediateAnalysis()}
        sentimentService={new FailingSentiment()}
      />,
    );
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
    });
    await waitFor(() =>
      expect(screen.getByText('ANALYSIS UNAVAILABLE')).toBeTruthy(),
    );

    const sentiment = within(region('Sentiment analysis'));
    expect(sentiment.getByText('ANALYSIS UNAVAILABLE')).toBeTruthy();
    expect(sentiment.getByRole('button', { name: /^retry$/i })).toBeTruthy();
    // Request error: the truthful status row in the Overview identity card.
    expect(within(region('Overview')).getByText('Analysis unavailable')).toBeTruthy();
    // Honest pending copy elsewhere - no invented percentages.
    expect(
      within(region('Emotion intelligence')).getByText(
        /appear here when the analysis completes/,
      ),
    ).toBeTruthy();
    expect(screen.queryByText('60%')).toBeNull();
  });

  it('renders no nav before acquisition (status views stay full-screen)', () => {
    const store = createStore();
    render(
      <App
        store={store}
        analysisService={new ImmediateAnalysis()}
        sentimentService={new PendingSentiment()}
      />,
    );
    // Opened but not yet acquired: no section bar, only the status view.
    expect(screen.queryByRole('navigation', { name: 'Intelligence sections' })).toBeNull();
    void store;
  });
});
