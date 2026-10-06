// @vitest-environment jsdom
/**
 * Sprint 7 §40 - AI audience insight UI tests.
 *
 * Every body below is backend-shaped (camelCase InsightAnalysis): the
 * section must render exactly what the transport delivered, show the
 * documented loading/failure/insufficient states (§30), expose the Why?
 * evidence (§20) with insight -> topic linking (§21), label the source
 * honestly (§23 - only a real LLM run says AI-generated), and never
 * surface Video A's insight under Video B (§26/§27).
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
import { afterEach, describe, expect, it } from 'vitest';
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
  InsightAnalysis,
  InsightCard,
  SentimentAnalysis,
  TopicAnalysis,
  VideoDataResponse,
} from '../shared/types';
import { createStore, type Store } from '../state/store';
import { App } from './App';

afterEach(() => {
  cleanup();
});

const VIDEO_URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
const VIDEO_B_URL = 'https://www.youtube.com/watch?v=BBBBBBBBBBB';
const VIDEO = 'dQw4w9WgXcQ';
const VIDEO_B = 'BBBBBBBBBBB';

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
    comments: { items: [], count: 1211, hasMore: false, status: 'ok' },
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

function card(
  category: InsightCard['category'],
  title: string,
  body: string,
  evidence: InsightCard['evidence'] = [],
): InsightCard {
  return { category, title, body, evidence };
}

function insightFor(videoId: string, headline = 'The audience response is strongly positive'): InsightAnalysis {
  return {
    videoId,
    status: 'READY',
    message: null,
    headline,
    summary:
      'The analyzed audience shows 68.3% positive, 29.0% neutral, 2.7% negative across 1,211 analyzed comments. Viewers repeatedly praised Clear explanations (284 mentions, 91.0% positive).',
    cards: [
      card(
        'OVERALL_REACTION',
        headline,
        'The analyzed audience shows 68.3% positive across 1,211 analyzed comments.',
        [
          {
            kind: 'SENTIMENT',
            label: 'Overall sentiment',
            value: '68.3% positive',
            detail: '29.0% neutral · 2.7% negative',
            topicId: null,
          },
          {
            kind: 'SAMPLE',
            label: 'Evidence base',
            value: '1211 analyzed comments',
            detail: '2429 collected · 1218 skipped',
            topicId: null,
          },
        ],
      ),
      card(
        'WHAT_WORKED',
        'Clear explanations',
        'Viewers repeatedly praised Clear explanations (284 mentions, 91.0% positive).',
        [
          {
            kind: 'APPRECIATED',
            label: 'Clear explanations',
            value: '284 mentions',
            detail: '91.0% positive',
            topicId: 'clear-explanations',
          },
        ],
      ),
      card(
        'MAIN_DISCUSSION',
        'Learning roadmap',
        'Learning roadmap is the main discussion theme (312 mentions, 25.8% of analyzed comments).',
        [
          {
            kind: 'TOPIC',
            label: 'Learning roadmap',
            value: '312 mentions',
            detail: '25.8% of analyzed',
            topicId: 'learning-roadmap',
          },
        ],
      ),
      card(
        'PAIN_POINT',
        'Learning difficulty',
        'Some viewers expressed recurring frustration with Learning difficulty (96 mentions, 58.0% negative).',
      ),
      card(
        'EMOTIONAL_SIGNAL',
        'TRUST',
        'TRUST is the dominant detected emotion (37.7% of analyzed comments), associated with the dominant positive sentiment.',
      ),
      card(
        'TAKEAWAY',
        'Key takeaway',
        'Overall, the audience response is strongly positive, with the strongest appreciation centered on Clear explanations.',
      ),
    ],
    sample: { collected: 2429, analyzed: 1211, skipped: 1218 },
    source: 'deterministic',
    provider: { name: 'deterministic', model: null },
    evidenceVersion: '1211:stamp',
    generatedAt: '2026-10-01T12:00:00Z',
    generationMs: 3,
  };
}

function topicsBody(videoId: string): TopicAnalysis {
  const topic = {
    topicId: 'clear-explanations',
    label: 'Clear explanations',
    mentions: 284,
    sharePercent: 23.5,
    keyPhrases: ['Clear explanations'],
    category: 'APPRECIATED' as const,
    confidence: 0.8,
    evidence: 'Frequently praised',
    sentiment: {
      POSITIVE: { count: 258, percent: 90.8 },
      NEUTRAL: { count: 20, percent: 7 },
      NEGATIVE: { count: 6, percent: 2.1 },
    },
    dominantSentiment: 'POSITIVE' as const,
  };
  return {
    videoId,
    status: 'READY',
    analyzedComments: 1211,
    message: null,
    topics: [topic],
    mostDiscussed: [],
    mostAppreciated: [topic],
    painPoints: [],
    mixedTopics: [],
  };
}

/** Transport returning per-call scripted results (records every call). */
class ScriptedInsight implements InsightService {
  readonly requests: InsightServiceRequest[] = [];
  constructor(
    private readonly responder: (
      request: InsightServiceRequest,
      call: number,
    ) => InsightServiceResult | Promise<InsightServiceResult>,
  ) {}
  getInsight(request: InsightServiceRequest): Promise<InsightServiceResult> {
    const call = this.requests.length;
    this.requests.push(request);
    return Promise.resolve(this.responder(request, call));
  }
}

class ScriptedTopics implements TopicsService {
  getTopics(request: TopicsServiceRequest): Promise<TopicsServiceResult> {
    return Promise.resolve({ kind: 'success', data: topicsBody(request.videoId) });
  }
}

class ImmediateAnalysis implements AnalysisService {
  analyze(request: AnalysisServiceRequest): Promise<AnalysisServiceResult> {
    return Promise.resolve({ kind: 'success', data: dataFor(request.videoId) });
  }
}

class ScriptedSentiment implements SentimentService {
  constructor(private readonly body: SentimentAnalysis) {}
  getSentiment(_request: SentimentServiceRequest): Promise<SentimentServiceResult> {
    return Promise.resolve({ kind: 'success', data: this.body });
  }
}

function renderFlow({
  insight,
  topics,
  sentiment = sentimentBody(VIDEO),
  url = VIDEO_URL,
}: {
  insight?: InsightService;
  topics?: TopicsService;
  sentiment?: SentimentAnalysis;
  url?: string;
}): Store {
  const store = createStore();
  render(
    <App
      store={store}
      analysisService={new ImmediateAnalysis()}
      sentimentService={new ScriptedSentiment(sentiment)}
      topicsService={topics}
      insightService={insight}
    />,
  );
  act(() => {
    store.setVideoContext(parseVideoContext(url));
  });
  return store;
}

describe('Sprint 7 - insight section rendering', () => {
  it('shows headline, summary, sample denominator and source honestly', async () => {
    renderFlow({
      insight: new ScriptedInsight(() => ({ kind: 'success', data: insightFor(VIDEO) })),
    });

    const section = await screen.findByRole('region', { name: 'AI Audience Insight' });
    expect(screen.getByText('The audience response is strongly positive')).toBeTruthy();
    expect(screen.getByText(/68\.3% positive/)).toBeTruthy();
    // §11: conclusions rest on ANALYZED comments (skipped shown separately).
    expect(within(section).getByText(/Based on 1,211 analyzed comments/)).toBeTruthy();
    expect(within(section).getByText(/1,218 skipped/)).toBeTruthy();
    // §23: deterministic source must NOT claim AI generation.
    expect(within(section).getByText('Data-derived')).toBeTruthy();
    expect(within(section).queryByText('AI-generated')).toBeNull();
    expect(
      screen.getByText('Derived from analyzed audience data.'),
    ).toBeTruthy();
    // §17: supporting cards render their one-sentence bodies (the phrase
    // also appears in the hero summary - both must be present).
    expect(
      screen.getAllByText(/Viewers repeatedly praised Clear explanations/).length,
    ).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/Learning roadmap is the main discussion theme/)).toBeTruthy();
    expect(screen.getByText(/recurring frustration with Learning difficulty/)).toBeTruthy();
    expect(screen.getByText(/TRUST is the dominant detected emotion/)).toBeTruthy();
    expect(screen.getByText(/strongest appreciation centered on Clear explanations/)).toBeTruthy();
  });

  it('labels a real LLM run as AI-generated (§23)', async () => {
    const body = {
      ...insightFor(VIDEO),
      source: 'llm' as const,
      provider: { name: 'openai_compatible', model: 'demo-model' },
    };
    renderFlow({ insight: new ScriptedInsight(() => ({ kind: 'success', data: body })) });
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    expect(screen.getByText('AI-generated')).toBeTruthy();
    expect(
      screen.getByText('Generated from analyzed audience data.'),
    ).toBeTruthy();
  });

  it('labels an LLM failure fallback as data-derived (§23/§45)', async () => {
    const body = {
      ...insightFor(VIDEO),
      source: 'fallback' as const,
      provider: { name: 'deterministic', model: null },
    };
    renderFlow({ insight: new ScriptedInsight(() => ({ kind: 'success', data: body })) });
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    expect(screen.getByText('Data-derived')).toBeTruthy();
    expect(
      screen.getByText('AI generation was unavailable - text derived from the analysis.'),
    ).toBeTruthy();
  });

  it('requests the insight once per video (§37 - no re-hammering)', async () => {
    const service = new ScriptedInsight(() => ({
      kind: 'success',
      data: insightFor(VIDEO),
    }));
    renderFlow({ insight: service });
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    await waitFor(() => expect(service.requests).toHaveLength(1));
    // Re-renders (progressive state ticks) must not refetch.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(service.requests).toHaveLength(1);
  });
});

describe('Sprint 7 - insight lifecycle states (§30)', () => {
  it('shows the waiting copy while the background job is still running', async () => {
    renderFlow({
      insight: new ScriptedInsight(
        () =>
          new Promise<InsightServiceResult>((resolve) =>
            setTimeout(() => resolve({ kind: 'success', data: insightFor(VIDEO) }), 400),
          ),
      ),
    });
    // Job is idle here, so the request-phase copy shows.
    const status = await screen.findByText('Connecting the audience signals…');
    expect(status.closest('[role="status"]')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'AI Audience Insight' })).toBeNull();
  });

  it('keeps sentiment and topics visible when the insight fails, with Retry (§30)', async () => {
    const service = new ScriptedInsight((_request, call) =>
      call === 0
        ? {
            kind: 'error',
            code: 'network_error',
            message: 'Backend is unreachable. Start the local backend (see README) and try again.',
          }
        : { kind: 'success', data: insightFor(VIDEO) },
    );
    renderFlow({ insight: service, topics: new ScriptedTopics() });

    await screen.findByText('AUDIENCE INSIGHT UNAVAILABLE');
    // Sentiment above + topics below survive the failure untouched (§30).
    const sentimentRegion = screen.getByRole('region', { name: 'Sentiment analysis' });
    expect(within(sentimentRegion).getAllByText(/68\.3/).length).toBeGreaterThan(0);
    expect(screen.getByRole('region', { name: 'Discussion topics' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    expect(service.requests).toHaveLength(2);
    expect(screen.queryByText('AUDIENCE INSIGHT UNAVAILABLE')).toBeNull();
  });

  it('renders the honest insufficient-data copy without a summary (§10)', async () => {
    const body: InsightAnalysis = {
      ...insightFor(VIDEO),
      status: 'INSUFFICIENT_DATA',
      message: 'Not enough analyzed audience evidence to generate a reliable insight.',
      headline: '',
      summary: '',
      cards: [],
    };
    renderFlow({ insight: new ScriptedInsight(() => ({ kind: 'success', data: body })) });
    await screen.findByText('AUDIENCE INSIGHT');
    expect(
      screen.getByText('Not enough analyzed audience evidence to generate a reliable insight.'),
    ).toBeTruthy();
    expect(
      screen.queryByText('The audience response is strongly positive'),
    ).toBeNull();
  });

  it('renders nothing when no insight transport is configured (§5)', async () => {
    renderFlow({ topics: new ScriptedTopics() });
    await screen.findByRole('region', { name: 'Discussion topics' });
    expect(screen.queryByRole('region', { name: 'AI Audience Insight' })).toBeNull();
  });

  it('hides the section while minimized and restores it (§40)', async () => {
    const store = renderFlow({
      insight: new ScriptedInsight(() => ({ kind: 'success', data: insightFor(VIDEO) })),
    });
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    act(() => {
      store.minimize();
    });
    expect(screen.queryByRole('region', { name: 'AI Audience Insight' })).toBeNull();
    act(() => {
      store.restore();
    });
    expect(screen.getByRole('region', { name: 'AI Audience Insight' })).toBeTruthy();
  });
});

describe('Sprint 7 - evidence interaction (§20/§21)', () => {
  it('opens the Why? panel with real evidence lines for the hero', async () => {
    renderFlow({
      insight: new ScriptedInsight(() => ({ kind: 'success', data: insightFor(VIDEO) })),
    });
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    const why = screen.getAllByRole('button', { name: 'Why?' });
    fireEvent.click(why[0]);
    expect(screen.getByText('Overall sentiment')).toBeTruthy();
    expect(screen.getByText('68.3% positive')).toBeTruthy();
    expect(screen.getByText('Evidence base')).toBeTruthy();
    expect(screen.getByText('2429 collected · 1218 skipped')).toBeTruthy();
  });

  it('links an insight evidence line to its underlying topic row (§21)', async () => {
    renderFlow({
      insight: new ScriptedInsight(() => ({ kind: 'success', data: insightFor(VIDEO) })),
      topics: new ScriptedTopics(),
    });
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    await screen.findByRole('region', { name: 'Discussion topics' });

    // Open the What Worked card's Why? panel and click its topic line.
    const whyButtons = screen.getAllByRole('button', { name: 'Why?' });
    fireEvent.click(whyButtons[1]); // WHAT_WORKED card
    const link = await screen.findByRole('button', { name: /view topic/i });
    fireEvent.click(link);

    // The underlying topic row expands and glows (data-highlight).
    await waitFor(() => {
      const row = document.querySelector('[data-topic-id="clear-explanations"]');
      expect(row).toBeTruthy();
      expect(row!.getAttribute('data-highlight')).toBe('true');
      expect(row!.getAttribute('data-expanded')).toBe('true');
    });
  });

  it('collapses the Why? panel when the slice changes (§27 video switch)', async () => {
    const service = new ScriptedInsight((request) => ({
      kind: 'success',
      data: insightFor(request.videoId, 'The audience response is mostly positive'),
    }));
    const store = renderFlow({ insight: service });
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    fireEvent.click(screen.getAllByRole('button', { name: 'Why?' })[0]);
    expect(screen.getByText('Overall sentiment')).toBeTruthy();

    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_B_URL));
    });
    // B's fresh slice: idle first, then B's own insight - A's expanded
    // evidence never leaks across.
    expect(screen.queryByText('Overall sentiment')).toBeNull();
    await waitFor(() => expect(service.requests.length).toBeGreaterThanOrEqual(2));
    expect(service.requests[service.requests.length - 1].videoId).toBe(VIDEO_B);
  });
});

describe('Sprint 7 - active-video safety (§26/§27)', () => {
  it('never renders Video A insight under Video B', async () => {
    const service = new ScriptedInsight((request) => ({
      kind: 'success',
      data: insightFor(request.videoId, `Insight for ${request.videoId}`),
    }));
    const store = renderFlow({ insight: service });
    await screen.findByRole('region', { name: 'AI Audience Insight' });
    expect(screen.getByText(`Insight for ${VIDEO}`)).toBeTruthy();

    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_B_URL));
    });
    await waitFor(() => {
      expect(screen.getByText(`Insight for ${VIDEO_B}`)).toBeTruthy();
    });
    expect(screen.queryByText(`Insight for ${VIDEO}`)).toBeNull();
  });

  it('discards a stale insight response that lands after navigation (§26)', async () => {
    let resolveA: ((result: InsightServiceResult) => void) | null = null;
    const service = new ScriptedInsight((request) => {
      if (request.videoId === VIDEO) {
        return new Promise<InsightServiceResult>((resolve) => {
          resolveA = resolve;
        });
      }
      return { kind: 'success', data: insightFor(VIDEO_B, 'Insight for B') };
    });
    const store = renderFlow({ insight: service });
    await waitFor(() => expect(service.requests).toHaveLength(1));

    // Navigate while A's request is still in flight, then let it land.
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_B_URL));
    });
    await waitFor(() => expect(service.requests.length).toBeGreaterThanOrEqual(2));
    await act(async () => {
      resolveA?.({ kind: 'success', data: insightFor(VIDEO, 'Insight for A') });
      await Promise.resolve();
    });

    await screen.findByText('Insight for B');
    expect(screen.queryByText('Insight for A')).toBeNull();
    // Store-side guard: A's late result was discarded, not applied.
    expect(store.getState().insight.data?.videoId).toBe(VIDEO_B);
  });
});
