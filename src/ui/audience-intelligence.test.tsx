// @vitest-environment jsdom
/**
 * Sprint 5 UI (§17/§25): the intelligence cards render from REAL backend
 * blocks, and never exist without them.
 *
 * - Full Sprint 5 body -> Audience Emotion, Audience Mood, Emotional
 *   Energy (with counts), Analysis Quality + the unchanged sentiment /
 *   coverage sections
 * - Legacy body (no Sprint 5 blocks) -> no intelligence cards at all
 * - Explicit nulls (confidence unavailable / too-small mood sample) ->
 *   exactly those cards disappear, the rest stays
 * - Zero-click still holds: results arrive automatically, no Analyze
 *   control anywhere (§3/§16)
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
import type {
  SentimentAnalysis,
  VideoDataResponse,
} from '../shared/types';
import { createStore, type Store } from '../state/store';
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

/** Full Sprint 5 body: real vocabulary, counts sum to analyzed (10). */
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

/** Sentiment transport returning the given backend body as-is. */
class ScriptedSentiment implements SentimentService {
  constructor(private readonly body: SentimentAnalysis) {}
  getSentiment(_request: SentimentServiceRequest): Promise<SentimentServiceResult> {
    return Promise.resolve({ kind: 'success', data: this.body });
  }
}

/** Renders the app closed, navigates to the video, waits for results. */
async function renderResults(body: SentimentAnalysis): Promise<Store> {
  const store = createStore();
  render(
    <App
      store={store}
      analysisService={new ImmediateAnalysis()}
      sentimentService={new ScriptedSentiment(body)}
    />,
  );
  act(() => {
    store.setVideoContext(parseVideoContext(VIDEO_URL));
  });
  await waitFor(() => expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy());
  return store;
}

function assertNoManualAnalyzeControls(): void {
  expect(screen.queryByRole('button', { name: /^analyze$/i })).toBeNull();
  expect(screen.queryByRole('button', { name: /analyze again/i })).toBeNull();
  expect(screen.queryByRole('button', { name: /run analysis/i })).toBeNull();
}

describe('Sprint 5 intelligence cards (§17)', () => {
  it('renders every intelligence card from a full backend body', async () => {
    await renderResults(intelligenceBody(VIDEO));

    // Unchanged sentiment + coverage sections.
    expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    expect(screen.getByText(/dominant sentiment/i)).toBeTruthy();
    // 60% shows twice by design: dominant verdict card + positive bar.
    expect(screen.getAllByText('60%').length).toBe(2);
    expect(screen.getByText(/Based on 10 analyzed comments/)).toBeTruthy();

    // Dominant emotion card: real label + its share of analyzed, now in the
    // Emotion section - mirrored by the Overview glance card (summary ↔
    // detail: same backend value, only one visible at a time).
    expect(screen.getByText('Audience Emotion')).toBeTruthy();
    expect(screen.getAllByText('JOY').length).toBe(2); // glance tile + card
    const emotion = within(
      screen.getByRole('region', { name: 'Emotion intelligence' }),
    );
    expect(emotion.getByText('JOY')).toBeTruthy();
    expect(emotion.getByText('40%')).toBeTruthy();
    const overview = within(screen.getByRole('region', { name: 'Overview' }));
    expect(overview.getByText('JOY')).toBeTruthy();
    expect(overview.getByText('40%')).toBeTruthy();
    // §8/§13: model terminology hides behind Technical Information instead
    // of sitting under the emotion card - expand to verify it still exists.
    expect(screen.queryByText(/NRC emotion model/i)).toBeNull();
    fireEvent.click(
      screen.getByRole('button', { name: /technical information/i }),
    );
    expect(screen.getByText(/NRC emotion model/i)).toBeTruthy();

    // Audience mood card (derived, documented) - in the Emotion section and
    // echoed by the Overview MOOD glance card.
    expect(screen.getByText('Audience Mood')).toBeTruthy();
    expect(emotion.getByText('EXCITED')).toBeTruthy();
    expect(overview.getByText('EXCITED')).toBeTruthy();
    expect(
      screen.getByText(/derived from analyzed sentiment, emotion & intensity/i),
    ).toBeTruthy();

    // Emotional intensity: all three bands with percent AND count (never
    // color-only), plus the overall indicator.
    expect(screen.getByText('Emotional Energy')).toBeTruthy();
    expect(screen.getByText('Low')).toBeTruthy();
    expect(screen.getByText('Medium')).toBeTruthy();
    expect(screen.getByText('High')).toBeTruthy();
    expect(emotion.getByText('MEDIUM')).toBeTruthy(); // overall badge
    expect(overview.getByText('MEDIUM')).toBeTruthy(); // ENERGY glance tile
    expect(screen.getByText('30%')).toBeTruthy(); // intensity LOW
    expect(screen.getByText('50%')).toBeTruthy(); // intensity MEDIUM
    // '20%' appears for negative sentiment AND intensity HIGH - both real.
    expect(screen.getAllByText('20%').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/Overall MEDIUM · based on/)).toBeTruthy();

    // Model confidence with the honest decision-margin caption.
    expect(screen.getByText('Analysis Quality')).toBeTruthy();
    expect(screen.getByText('84%')).toBeTruthy();
    expect(screen.getByText('sentiment decision margin')).toBeTruthy();

    assertNoManualAnalyzeControls();
  });

  it('renders NO intelligence cards for a pre-Sprint-5 backend body', async () => {
    await renderResults(baseBody(VIDEO));

    // Sentiment section still works exactly as before.
    expect(screen.getByText('ANALYSIS COMPLETE')).toBeTruthy();
    expect(screen.getByText(/Based on 10 analyzed comments/)).toBeTruthy();

    // Cards never appear without their backend block - nothing invented.
    expect(screen.queryByText('Audience Emotion')).toBeNull();
    expect(screen.queryByText('Audience Mood')).toBeNull();
    expect(screen.queryByText('Emotional Energy')).toBeNull();
    expect(screen.queryByText('Analysis Quality')).toBeNull();
    expect(screen.queryByText('EXCITED')).toBeNull();
    expect(screen.queryByText('JOY')).toBeNull();
    assertNoManualAnalyzeControls();
  });

  it('hides exactly the unavailable confidence card when average is null', async () => {
    const body = { ...intelligenceBody(VIDEO), confidence: { average: null } };
    await renderResults(body);

    expect(screen.queryByText('Analysis Quality')).toBeNull();
    // Everything else still renders.
    expect(screen.getByText('Audience Emotion')).toBeTruthy();
    expect(screen.getByText('Audience Mood')).toBeTruthy();
    expect(screen.getByText('Emotional Energy')).toBeTruthy();
  });

  it('hides the mood card when the sample is too small for a mood', async () => {
    const body: SentimentAnalysis = {
      ...intelligenceBody(VIDEO),
      audienceMood: null,
    };
    await renderResults(body);

    expect(screen.queryByText('Audience Mood')).toBeNull();
    expect(screen.queryByText('EXCITED')).toBeNull();
    expect(screen.getByText('Audience Emotion')).toBeTruthy();
    expect(screen.getByText('Analysis Quality')).toBeTruthy();
  });

  it('never shows a mood/emotion without the corresponding backend value', async () => {
    const body = intelligenceBody(VIDEO);
    body.emotion = { ...body.emotion!, dominant: null, dominantPercent: 0 };
    await renderResults(body);

    // Card renders the honest em-dash placeholder, never an invented label.
    expect(screen.getByText('Audience Emotion')).toBeTruthy();
    expect(screen.getByText('—')).toBeTruthy();
    expect(screen.queryByText('JOY')).toBeNull();
  });
});
