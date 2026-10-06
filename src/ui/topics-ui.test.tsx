// @vitest-environment jsdom
/**
 * Sprint 6 §44 - topic / discussion intelligence UI tests.
 *
 * Every body below is backend-shaped (camelCase TopicAnalysis): the
 * section must render exactly what the transport delivered, show the
 * documented loading/error/empty states (§36/§38/§39), expand inline
 * (§34), and never surface Video A's topics under Video B (§40).
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
  EmotionBreakdown,
  IntensityBreakdown,
  OverlayState,
  SentimentAnalysis,
  TopicAnalysis,
  TopicItem,
  VideoDataResponse,
} from '../shared/types';
import { createStore } from '../state/store';
import { App } from './App';
import { OverlayBody } from './components/OverlayBody';

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
    comments: { items: [], count: 10, hasMore: false, status: 'ok' },
    source: { provider: 'youtube', retrievedAt: '2026-09-27T00:00:00Z', cached: false },
  };
}

function sentimentBody(videoId: string): SentimentAnalysis {
  return {
    videoId,
    status: 'PROCESSED',
    stats: {
      totalComments: 1211,
      analyzed: 1211,
      skipped: 0,
      positive: 827,
      neutral: 243,
      negative: 141,
      positivePercent: 68.3,
      neutralPercent: 20.1,
      negativePercent: 11.6,
    },
    dataset: {
      collected: 1211,
      stored: 1211,
      analyzed: 1211,
      skipped: 0,
      failed: 0,
      hasMore: false,
      limitReached: false,
    },
    dominantSentiment: 'POSITIVE',
  };
}

const TRUST_EMOTION: EmotionBreakdown = {
  dominant: 'TRUST',
  dominantPercent: 37.7,
  distribution: {
    FEAR: { count: 40, percent: 3.3 },
    ANGER: { count: 30, percent: 2.5 },
    ANTICIPATION: { count: 90, percent: 7.4 },
    TRUST: { count: 457, percent: 37.7 },
    SURPRISE: { count: 60, percent: 5 },
    SADNESS: { count: 50, percent: 4.1 },
    DISGUST: { count: 24, percent: 2 },
    JOY: { count: 300, percent: 24.8 },
    NEUTRAL: { count: 160, percent: 13.2 },
  },
};

const LOW_INTENSITY: IntensityBreakdown = {
  overall: 'LOW',
  distribution: {
    LOW: { count: 476, percent: 39.3 },
    MEDIUM: { count: 392, percent: 32.4 },
    HIGH: { count: 343, percent: 28.3 },
  },
};

function shares(count: number, mentions: number) {
  const pct = (n: number) => Math.round((n / mentions) * 1000) / 10;
  return {
    POSITIVE: { count, percent: pct(count) },
    NEUTRAL: { count: 0, percent: 0 },
    NEGATIVE: { count: 0, percent: 0 },
  };
}

function makeTopic(
  overrides: Partial<TopicItem> & Pick<TopicItem, 'topicId' | 'label' | 'mentions' | 'category'>,
): TopicItem {
  const mentions = overrides.mentions;
  const base = {
    sharePercent: Math.round((mentions / 1211) * 1000) / 10,
    keyPhrases: [overrides.label],
    confidence: 0.72,
    evidence: 'Recurring discussion',
    sentiment: shares(Math.floor(mentions * 0.7), mentions),
    dominantSentiment: 'POSITIVE' as const,
    emotion: TRUST_EMOTION,
    intensity: LOW_INTENSITY,
    ...overrides,
  };
  return base as TopicItem;
}

const ROADMAP = makeTopic({
  topicId: 'learning-roadmap',
  label: 'Learning roadmap',
  mentions: 312,
  category: 'MOST_DISCUSSED',
  keyPhrases: ['Learning roadmap', 'Clear roadmap'],
  sentiment: {
    POSITIVE: { count: 262, percent: 84 },
    NEUTRAL: { count: 35, percent: 11.2 },
    NEGATIVE: { count: 15, percent: 4.8 },
  },
  evidence: 'Recurring discussion: 312 mentions (25.8% of analyzed comments)',
});

const PROJECTS = makeTopic({
  topicId: 'react-projects',
  label: 'React projects',
  mentions: 241,
  category: 'MOST_DISCUSSED',
  sentiment: {
    POSITIVE: { count: 150, percent: 62.2 },
    NEUTRAL: { count: 60, percent: 24.9 },
    NEGATIVE: { count: 31, percent: 12.9 },
  },
});

const EXPLANATIONS = makeTopic({
  topicId: 'clear-explanations',
  label: 'Clear explanations',
  mentions: 284,
  category: 'APPRECIATED',
  sentiment: {
    POSITIVE: { count: 258, percent: 90.8 },
    NEUTRAL: { count: 20, percent: 7 },
    NEGATIVE: { count: 6, percent: 2.1 },
  },
  evidence: 'Frequently praised: 91% positive across 284 mentions',
});

const SETUP = makeTopic({
  topicId: 'setup-problems',
  label: 'Setup problems',
  mentions: 96,
  category: 'PAIN_POINT',
  dominantSentiment: 'NEGATIVE',
  sentiment: {
    POSITIVE: { count: 34, percent: 35.4 },
    NEUTRAL: { count: 6, percent: 6.3 },
    NEGATIVE: { count: 56, percent: 58.3 },
  },
  evidence: 'Repeatedly discussed negative theme: 58% negative across 96 mentions',
});

const COMPLEXITY = makeTopic({
  topicId: 'react-complexity',
  label: 'React complexity',
  mentions: 184,
  category: 'MIXED',
  sentiment: {
    POSITIVE: { count: 79, percent: 42.9 },
    NEUTRAL: { count: 39, percent: 21.2 },
    NEGATIVE: { count: 66, percent: 35.9 },
  },
  evidence: 'Mixed discussion: 43% positive, 36% negative across 184 mentions',
});

function topicsFor(videoId: string, topics: TopicItem[]): TopicAnalysis {
  const byCategory = (category: string) =>
    topics.filter((topic) => topic.category === category);
  return {
    videoId,
    status: 'READY',
    analyzedComments: 1211,
    message: null,
    topics,
    mostDiscussed: byCategory('MOST_DISCUSSED'),
    mostAppreciated: byCategory('APPRECIATED'),
    painPoints: byCategory('PAIN_POINT'),
    mixedTopics: byCategory('MIXED'),
  };
}

function fullTopics(videoId: string): TopicAnalysis {
  return topicsFor(videoId, [ROADMAP, PROJECTS, EXPLANATIONS, SETUP, COMPLEXITY]);
}

/** Transport returning per-call scripted results (records every call). */
class ScriptedTopics implements TopicsService {
  readonly requests: TopicsServiceRequest[] = [];
  constructor(
    private readonly responder: (
      request: TopicsServiceRequest,
      call: number,
    ) => TopicsServiceResult | Promise<TopicsServiceResult>,
  ) {}
  getTopics(request: TopicsServiceRequest): Promise<TopicsServiceResult> {
    const call = this.requests.length;
    this.requests.push(request);
    return Promise.resolve(this.responder(request, call));
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

function renderFlow(
  topics: TopicsService,
  sentiment: SentimentAnalysis = sentimentBody(VIDEO),
  url = VIDEO_URL,
) {
  const store = createStore();
  render(
    <App
      store={store}
      analysisService={new ImmediateAnalysis()}
      sentimentService={new ScriptedSentiment(sentiment)}
      topicsService={topics}
    />,
  );
  act(() => {
    store.setVideoContext(parseVideoContext(url));
  });
  return store;
}

describe('Sprint 6 - discussion section rendering', () => {
  it('shows the four ranked groups with backend counts and rank indicators', async () => {
    const service = new ScriptedTopics(() => ({
      kind: 'success',
      data: fullTopics(VIDEO),
    }));
    renderFlow(service);

    const section = await screen.findByRole('region', {
      name: 'Discussion topics',
    });
    expect(screen.getByText('What People Talk About')).toBeTruthy();
    expect(
      screen.getByText('Discovered from 1,211 analyzed comments'),
    ).toBeTruthy();

    // Group headings (§30) with their items.
    expect(screen.getByText('Most Discussed')).toBeTruthy();
    expect(screen.getByText('What People Loved')).toBeTruthy();
    expect(screen.getByText('What People Struggle With')).toBeTruthy();
    expect(screen.getByText('Mixed Discussions')).toBeTruthy();

    // Frequency + ratios exactly as delivered (§31-§33).
    expect(within(section).getByText('Learning roadmap')).toBeTruthy();
    expect(within(section).getByText(/312 mentions · 25\.8% of analyzed/)).toBeTruthy();
    expect(within(section).getByText(/284 mentions · 90\.8% positive/)).toBeTruthy();
    expect(within(section).getByText(/96 mentions · 58\.3% negative/)).toBeTruthy();
    expect(
      within(section).getByText(/184 mentions · 42\.9% positive \/ 35\.9% negative/),
    ).toBeTruthy();

    // Subtle ranking indicators (01/02) on Most Discussed only.
    expect(screen.getByText('01')).toBeTruthy();
    expect(screen.getByText('02')).toBeTruthy();
    expect(screen.queryByText('03')).toBeNull();
  });

  it('renders frequency bars sized by the group maximum', async () => {
    const service = new ScriptedTopics(() => ({
      kind: 'success',
      data: fullTopics(VIDEO),
    }));
    const store = renderFlow(service);
    await screen.findByRole('region', { name: 'Discussion topics' });
    const bars = document.querySelectorAll('.sai-topic-fill');
    expect(bars.length).toBeGreaterThanOrEqual(3); // discussed + loved + pain
    expect((bars[0] as HTMLElement).style.width).toBe('100%'); // 312/312 max
    // Mixed topics use the stacked sentiment bar (§15).
    const mixbar = document.querySelector('.sai-topic-mixbar');
    expect(mixbar).toBeTruthy();
    expect(mixbar!.querySelectorAll('span').length).toBe(3);
    act(() => {
      store.minimize();
    });
  });
});

describe('Sprint 6 - topic lifecycle states', () => {
  it('shows the discovering state while the request is in flight', async () => {
    const service = new ScriptedTopics(
      () =>
        new Promise<TopicsServiceResult>((resolve) =>
          setTimeout(
            () => resolve({ kind: 'success', data: fullTopics(VIDEO) }),
            400,
          ),
        ),
    );
    renderFlow(service);
    const status = await screen.findByText('Discovering audience discussions…');
    expect(status.closest('[role="status"]')).toBeTruthy();
    expect(screen.queryByText('What People Talk About')).toBeNull();
  });

  it('keeps sentiment visible when topics fail, with its own retry (§39)', async () => {
    const service = new ScriptedTopics((_request, call) =>
      call === 0
        ? {
            kind: 'error',
            code: 'network_error',
            message: 'Backend is unreachable. Start the local backend (see README) and try again.',
          }
        : { kind: 'success', data: fullTopics(VIDEO) },
    );
    renderFlow(service);

    await screen.findByText('AUDIENCE TOPICS UNAVAILABLE');
    expect(
      screen.getByText('Sentiment, emotion and intensity results above are unaffected.'),
    ).toBeTruthy();
    // The sentiment hero is still there (a topic failure never hides it).
    expect(screen.getByText('Mostly Positive')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('What People Talk About');
    expect(service.requests.length).toBe(2);
  });

  it('shows the honest empty copy when discovery finds nothing (§38)', async () => {
    const service = new ScriptedTopics(() => ({
      kind: 'success',
      data: {
        ...topicsFor(VIDEO, []),
        message: 'Not enough repeated discussion yet.',
      },
    }));
    renderFlow(service);
    await screen.findByText('Not enough repeated discussion yet.');
    expect(screen.queryByText('Most Discussed')).toBeNull();
  });

  it('shows the insufficient-sample copy below the backend minimum (§38)', async () => {
    const service = new ScriptedTopics(() => ({
      kind: 'success',
      data: {
        videoId: VIDEO,
        status: 'INSUFFICIENT_DATA',
        analyzedComments: 8,
        message: 'More audience responses are needed to identify repeated themes.',
        topics: [],
        mostDiscussed: [],
        mostAppreciated: [],
        painPoints: [],
        mixedTopics: [],
      },
    }));
    renderFlow(service);
    await screen.findByText(
      'More audience responses are needed to identify repeated themes.',
    );
    expect(screen.queryByText('What People Talk About')).toBeNull();
  });

  it('never fetches topics before the sentiment result is final', async () => {
    const service = new ScriptedTopics(() => ({
      kind: 'success',
      data: fullTopics(VIDEO),
    }));
    const processing: SentimentAnalysis = {
      ...sentimentBody(VIDEO),
      status: 'PROCESSING',
    };
    renderFlow(service, processing);
    // The processing view renders; the section and the transport stay idle.
    await screen.findByText('ANALYZING AUDIENCE');
    expect(service.requests.length).toBe(0);
    expect(screen.queryByText('What People Talk About')).toBeNull();
  });
});

describe('Sprint 6 - topic card expansion (§34)', () => {
  it('expands one card at a time with sentiment, emotion, intensity and evidence', async () => {
    const service = new ScriptedTopics(() => ({
      kind: 'success',
      data: fullTopics(VIDEO),
    }));
    renderFlow(service);
    await screen.findByRole('region', { name: 'Discussion topics' });

    const roadmapButton = screen.getByRole('button', {
      name: /Learning roadmap/,
    });
    expect(roadmapButton.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(roadmapButton);
    expect(roadmapButton.getAttribute('aria-expanded')).toBe('true');

    // §34 content: mentions → sentiment → dominant emotion → intensity.
    expect(
      screen.getByText(/312 mentions · 25\.8% of 1,211 analyzed comments/),
    ).toBeTruthy();
    expect(screen.getByText('Dominant emotion')).toBeTruthy();
    expect(screen.getByText(/TRUST · 37\.7%/)).toBeTruthy();
    expect(screen.getByText('Intensity')).toBeTruthy();
    expect(screen.getByText(/LOW · 39\.3% low/)).toBeTruthy();
    expect(screen.getByText('Topic confidence')).toBeTruthy();
    expect(screen.getByText('72%')).toBeTruthy();
    // §13: ideas, not raw comments; §20: evidence stays factual.
    expect(screen.getByText('Also said as')).toBeTruthy();
    expect(screen.getByText('Clear roadmap')).toBeTruthy();
    expect(
      screen.getByText('Recurring discussion: 312 mentions (25.8% of analyzed comments)'),
    ).toBeTruthy();

    // Opening a second card closes the first (single inline expansion).
    fireEvent.click(screen.getByRole('button', { name: /Setup problems/ }));
    expect(roadmapButton.getAttribute('aria-expanded')).toBe('false');
    expect(
      screen.queryByText(
        'Recurring discussion: 312 mentions (25.8% of analyzed comments)',
      ),
    ).toBeNull();

    // Clicking the open card again collapses it.
    const setupButton = screen.getByRole('button', { name: /Setup problems/ });
    fireEvent.click(setupButton);
    expect(setupButton.getAttribute('aria-expanded')).toBe('false');
  });
});

describe('Sprint 6 - active-video safety (§40)', () => {
  it('never surfaces Video A topics under Video B after a switch', async () => {
    let resolveA: (result: TopicsServiceResult) => void = () => {};
    const pendingA = new Promise<TopicsServiceResult>((resolve) => {
      resolveA = resolve;
    });
    const alphaTopics = topicsFor(VIDEO, [
      makeTopic({
        topicId: 'alpha-theme',
        label: 'Alpha theme',
        mentions: 120,
        category: 'MOST_DISCUSSED',
      }),
    ]);
    const service = new ScriptedTopics((request, call) => {
      if (call === 0) return pendingA; // Video A's slow response
      return { kind: 'success', data: fullTopics(request.videoId) };
    });
    const store = renderFlow(service);
    await waitFor(() => expect(service.requests.length).toBe(1)); // A's fetch in flight

    // A → B while A's topics are still pending.
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_B_URL));
    });
    // B runs its own flow and loads its own topics.
    await screen.findByText('What People Talk About');
    expect(screen.getByText('Learning roadmap')).toBeTruthy();

    // A's late response lands now - discarded by the store's video guard.
    await act(async () => {
      resolveA({ kind: 'success', data: alphaTopics });
      await pendingA;
    });
    expect(screen.queryByText('Alpha theme')).toBeNull();
    expect(screen.getByText('Learning roadmap')).toBeTruthy();
    expect(store.getState().topics.videoId).toBe(VIDEO_B);
  });

  it('drops topics for the previous video on context switch (slice reset)', async () => {
    const service = new ScriptedTopics((request) => ({
      kind: 'success',
      data: fullTopics(request.videoId),
    }));
    const store = renderFlow(service);
    await screen.findByText('What People Talk About');
    expect(store.getState().topics.status).toBe('success');

    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_B_URL));
    });
    // Fresh context: the old slice is gone before B's flow restarts.
    expect(store.getState().topics.status).toBe('idle');
    expect(store.getState().topics.data).toBeNull();
    expect(screen.queryByText('What People Talk About')).toBeNull();
  });
});

describe('Sprint 6 - minimized / restored overlay', () => {
  it('hides the section while minimized and restores it intact', async () => {
    const service = new ScriptedTopics(() => ({
      kind: 'success',
      data: fullTopics(VIDEO),
    }));
    const store = renderFlow(service);
    await screen.findByRole('region', { name: 'Discussion topics' });

    act(() => {
      store.minimize();
    });
    expect(screen.queryByText('What People Talk About')).toBeNull();

    act(() => {
      store.restore();
    });
    expect(screen.getByText('What People Talk About')).toBeTruthy();
  });
});

describe('Sprint 6/7 - in-flight phase copy (§36)', () => {
  const jobSnapshot = (phase: 'SENTIMENT' | 'TOPIC' | 'INSIGHT') => ({
    jobId: 'job-1',
    videoId: VIDEO,
    status: 'ANALYZING' as const,
    phase,
    collected: 1211,
    stored: 1211,
    analyzable: 1211,
    analyzed: 400,
    skipped: 0,
    failed: 0,
    pending: 811,
    hasMore: false,
    errorCode: null,
    errorMessage: null,
    createdAt: '2026-09-27T00:00:00Z',
    updatedAt: '2026-09-27T00:00:01Z',
    finishedAt: null,
  });

  function analyzingState(phase: 'SENTIMENT' | 'TOPIC' | 'INSIGHT'): OverlayState {
    return {
      status: 'analyzing',
      minimized: false,
      activeSection: 'overview',
      videoContext: null,
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
      topics: {
        status: 'idle',
        videoId: null,
        data: null,
        errorCode: null,
        errorMessage: null,
      },
      insight: {
        status: 'idle',
        videoId: null,
        data: null,
        errorCode: null,
        errorMessage: null,
      },
      job: {
        status: 'running',
        videoId: VIDEO,
        jobId: 'job-1',
        job: jobSnapshot(phase),
        errorCode: null,
        errorMessage: null,
      },
      realtime: {
        status: 'idle',
        videoId: null,
        data: null,
        errorCode: null,
        errorMessage: null,
      },
    };
  }

  it('names the real discovery step during the job TOPIC phase', () => {
    render(
      <OverlayBody
        state={analyzingState('TOPIC')}
        onRetry={() => {}}
        onSentimentRetry={() => {}}
        onTopicsRetry={() => {}}
        onInsightRetry={() => {}}
      />,
    );
    expect(screen.getByText('discovering discussions…')).toBeTruthy();
  });

  it('names the insight step during the job INSIGHT phase (Sprint 7)', () => {
    render(
      <OverlayBody
        state={analyzingState('INSIGHT')}
        onRetry={() => {}}
        onSentimentRetry={() => {}}
        onTopicsRetry={() => {}}
        onInsightRetry={() => {}}
      />,
    );
    expect(screen.getByText('building audience insight…')).toBeTruthy();
    expect(screen.queryByText('discovering discussions…')).toBeNull();
  });

  it('keeps the generic processing line during the sentiment phase', () => {
    render(
      <OverlayBody
        state={analyzingState('SENTIMENT')}
        onRetry={() => {}}
        onSentimentRetry={() => {}}
        onTopicsRetry={() => {}}
        onInsightRetry={() => {}}
      />,
    );
    expect(screen.getByText('processing…')).toBeTruthy();
    expect(screen.queryByText('discovering discussions…')).toBeNull();
  });
});

describe('Sprint 6 - no fabricated states', () => {
  it('renders no section when the transport is absent', async () => {
    const store = createStore();
    render(
      <App
        store={store}
        analysisService={new ImmediateAnalysis()}
        sentimentService={new ScriptedSentiment(sentimentBody(VIDEO))}
      />,
    );
    act(() => {
      store.setVideoContext(parseVideoContext(VIDEO_URL));
    });
    await waitFor(() => expect(screen.getByText('Mostly Positive')).toBeTruthy());
    expect(screen.queryByText('What People Talk About')).toBeNull();
    expect(store.getState().topics.status).toBe('idle');
  });
});
