/**
 * Sprint 5.2 §32 VISUAL-QA HARNESS (temporary - not part of the extension
 * build). Boots the REAL production mount path (shadow DOM + overlay.css?inline
 * + App) over the fake YouTube backdrop, using the §27 demo dataset:
 *
 *   collected 2,429 · analyzed 1,211 · skipped 1,218
 *   Positive 68.3% (827) · Neutral 29.0% (351) · Negative 2.7% (33)
 *   emotion TRUST 37.7% · intensity LOW (39.3/32.4/28.3) · confidence 71%
 *
 * Scenes via `?scene=`:
 *   complete   - PROCESSED results + Sprint 6/7 intelligence (default)
 *   acquiring  - acquisition in flight (spinner state)
 *   analyzing  - backend PROCESSING with real counts (§15 experience)
 *   failed     - categorized acquisition failure (Retry path)
 *   topics-error   - sentiment complete, topic transport failed (§39)
 *   topics-loading - topic transport pending (§36 discovering state)
 *   topics-empty   - READY with zero themes (§38 empty copy)
 *   insight-error  - insight transport failed (§30 Retry, sentiment intact)
 *   insight-loading- insight request pending (§30 states)
 *   insight-empty  - INSUFFICIENT_DATA honest copy (§10)
 *   insight-llm    - source=llm badge variant (§23)
 *   realtime-standby - monitor enabled but not running (STANDBY pill)
 *   realtime-off   - REALTIME_ENABLED=false (honest OFF pill)
 *   realtime-falling - FALLING trend + HIGH activity + queued comments
 *   realtime-error - /realtime transport failed (strip stays absent)
 *
 * Scroll behaviour is testable too: the context is aged, so a page scroll
 * minimizes the panel (OPEN → MINIMIZED) exactly like production.
 */
import overlayCss from '../src/ui/styles/overlay.css?inline';
import { mountApp } from '../src/ui/mount';
import { createStore } from '../src/state/store';
import { parseVideoContext } from '../src/services/youtube/video-context';
import type { AnalysisService, AnalysisServiceResult } from '../src/services/analysis/analysis-service';
import type { SentimentService, SentimentServiceResult } from '../src/services/analysis/sentiment-service';
import type { TopicsService, TopicsServiceRequest, TopicsServiceResult } from '../src/services/analysis/topics-service';
import type { InsightService, InsightServiceRequest, InsightServiceResult } from '../src/services/analysis/insight-service';
import type { RealtimeService, RealtimeServiceRequest, RealtimeServiceResult } from '../src/services/analysis/realtime-service';
import type { EmotionLabel, InsightAnalysis, RealtimeStatus, SentimentAnalysis, TopicAnalysis, TopicItem, VideoDataResponse } from '../src/shared/types';

const EMOTION_LABELS: readonly EmotionLabel[] = [
  'FEAR',
  'ANGER',
  'ANTICIPATION',
  'TRUST',
  'SURPRISE',
  'SADNESS',
  'DISGUST',
  'JOY',
  'NEUTRAL',
];

const EMOTION_COUNTS: Record<EmotionLabel, number> = {
  FEAR: 73,
  ANGER: 61,
  ANTICIPATION: 218,
  TRUST: 457,
  SURPRISE: 97,
  SADNESS: 73,
  DISGUST: 49,
  JOY: 145,
  NEUTRAL: 38,
};

const EMOTION_DISTRIBUTION = Object.fromEntries(
  EMOTION_LABELS.map((label) => [label, share(EMOTION_COUNTS[label], 1211)]),
) as Record<EmotionLabel, { count: number; percent: number }>;

const INTENSITY_COUNTS = { LOW: 476, MEDIUM: 392, HIGH: 343 } as const;

function share(count: number, denominator: number): { count: number; percent: number } {
  return { count, percent: Math.round((count / denominator) * 1000) / 10 };
}

const VIDEO: VideoDataResponse = {
  video: {
    videoId: 'vz1RlUyrc3w',
    title: 'React JS roadmap | chai aur react series',
    description: null,
    channelId: 'UCx-dKH8lRyciF0V3ryFDM4Q',
    channelTitle: 'Chai aur Code',
    publishedAt: '2024-05-14T10:00:00Z',
    categoryId: '27',
    duration: 'PT42M11S',
    statistics: { viewCount: 214338, likeCount: 9812, commentCount: 2429 },
  },
  comments: { items: [], count: 2429, hasMore: false, status: 'ok' },
  source: { provider: 'youtube', retrievedAt: '2026-09-30T21:40:00Z', cached: false },
};

const ANALYZED = 1211;

const RESULTS: SentimentAnalysis = {
  videoId: 'vz1RlUyrc3w',
  status: 'PROCESSED',
  stats: {
    totalComments: 2429,
    analyzed: ANALYZED,
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
    analyzed: ANALYZED,
    skipped: 1218,
    failed: 0,
    hasMore: false,
    limitReached: false,
  },
  dominantSentiment: 'POSITIVE',
  emotion: {
    dominant: 'TRUST',
    dominantPercent: 37.7,
    distribution: EMOTION_DISTRIBUTION,
  },
  intensity: {
    overall: 'LOW',
    distribution: {
      LOW: share(INTENSITY_COUNTS.LOW, ANALYZED),
      MEDIUM: share(INTENSITY_COUNTS.MEDIUM, ANALYZED),
      HIGH: share(INTENSITY_COUNTS.HIGH, ANALYZED),
    },
  },
  confidence: { average: 0.71 },
  audienceMood: 'POSITIVE',
};

const PROCESSING: SentimentAnalysis = {
  ...RESULTS,
  status: 'PROCESSING',
  stats: {
    ...RESULTS.stats,
    analyzed: 612,
    positive: 415,
    neutral: 156,
    negative: 41,
    positivePercent: 67.8,
    neutralPercent: 25.5,
    negativePercent: 6.7,
  },
  dataset: { ...RESULTS.dataset, analyzed: 612 },
  dominantSentiment: 'POSITIVE',
  emotion: { ...RESULTS.emotion! },
  intensity: { ...RESULTS.intensity! },
  confidence: { average: 0.7 },
};

const scene = new URLSearchParams(window.location.search).get('scene') ?? 'complete';

const analysisService: AnalysisService = {
  analyze(): Promise<AnalysisServiceResult> {
    if (scene === 'acquiring') {
      return new Promise<AnalysisServiceResult>(() => undefined);
    }
    if (scene === 'failed') {
      return Promise.resolve({
        kind: 'error',
        code: 'network_error',
        message: 'The local AI service could not be reached.',
      });
    }
    return Promise.resolve({ kind: 'success', data: VIDEO });
  },
};

const sentimentService: SentimentService = {
  getSentiment(): Promise<SentimentServiceResult> {
    return Promise.resolve({
      kind: 'success',
      data: scene === 'analyzing' ? PROCESSING : RESULTS,
    });
  },
};

// ---------------------------------------------------------------------------
// Sprint 6 §32: demo topic intelligence (CLEARLY SYNTHETIC test data, §27
// demo-set conventions) shaped exactly like GET /api/videos/{id}/topics.
// ---------------------------------------------------------------------------

function topic(
  topicId: string,
  label: string,
  mentions: number,
  category: TopicItem['category'],
  [pos, neu, neg]: [number, number, number],
  keyPhrases: string[],
  emotion: { dominant: string; dominantPercent: number },
  confidence: number,
  evidence: string,
): TopicItem {
  const pct = (n: number) => Math.round((n / mentions) * 1000) / 10;
  return {
    topicId,
    label,
    mentions,
    // Share of the ANALYZED denominator (§42), not of the topic itself.
    sharePercent: Math.round((mentions / ANALYZED) * 1000) / 10,
    keyPhrases,
    category,
    confidence,
    evidence,
    sentiment: {
      POSITIVE: { count: pos, percent: pct(pos) },
      NEUTRAL: { count: neu, percent: pct(neu) },
      NEGATIVE: { count: neg, percent: pct(neg) },
    },
    dominantSentiment:
      pos >= neu && pos >= neg ? 'POSITIVE' : neu >= neg ? 'NEUTRAL' : 'NEGATIVE',
    emotion: {
      dominant: emotion.dominant as EmotionLabel,
      dominantPercent: emotion.dominantPercent,
      distribution: EMOTION_DISTRIBUTION,
    },
    intensity: {
      overall: 'LOW',
      distribution: {
        LOW: share(INTENSITY_COUNTS.LOW, ANALYZED),
        MEDIUM: share(INTENSITY_COUNTS.MEDIUM, ANALYZED),
        HIGH: share(INTENSITY_COUNTS.HIGH, ANALYZED),
      },
    },
  } as TopicItem;
}

const DEMO_TOPICS: TopicItem[] = [
  topic('roadmap', 'Learning roadmap', 312, 'MOST_DISCUSSED', [264, 33, 15],
    ['Learning roadmap', 'Clear roadmap', 'Path to learn'],
    { dominant: 'TRUST', dominantPercent: 41.2 }, 0.74,
    'Recurring discussion: 312 mentions (25.8% of analyzed comments)'),
  topic('projects', 'React projects', 241, 'MOST_DISCUSSED', [142, 64, 35],
    ['React projects', 'Portfolio projects'],
    { dominant: 'ANTICIPATION', dominantPercent: 33.5 }, 0.69,
    'Recurring discussion: 241 mentions (19.9% of analyzed comments)'),
  topic('career', 'Career opportunities', 183, 'MOST_DISCUSSED', [120, 43, 20],
    ['Career opportunities', 'Jobs after react'],
    { dominant: 'TRUST', dominantPercent: 38.9 }, 0.71,
    'Recurring discussion: 183 mentions (15.1% of analyzed comments)'),
  topic('explanations', 'Clear explanations', 284, 'APPRECIATED', [258, 20, 6],
    ['Clear explanations', 'Easy to understand'],
    { dominant: 'TRUST', dominantPercent: 44.6 }, 0.81,
    'Frequently praised: 91% positive across 284 mentions'),
  topic('examples', 'Practical examples', 217, 'APPRECIATED', [189, 21, 7],
    ['Practical examples', 'Real world examples'],
    { dominant: 'JOY', dominantPercent: 39.1 }, 0.77,
    'Frequently praised: 87% positive across 217 mentions'),
  topic('structured', 'Structured roadmap', 193, 'APPRECIATED', [162, 24, 7],
    ['Structured roadmap', 'Step by step series'],
    { dominant: 'TRUST', dominantPercent: 42.3 }, 0.75,
    'Frequently praised: 84% positive across 193 mentions'),
  topic('difficulty', 'Learning difficulty', 96, 'PAIN_POINT', [34, 6, 56],
    ['Learning difficulty', 'Confusing concepts'],
    { dominant: 'FEAR', dominantPercent: 36.4 }, 0.66,
    'Repeatedly discussed negative theme: 58% negative across 96 mentions'),
  topic('volume', 'Too much content', 71, 'PAIN_POINT', [25, 13, 33],
    ['Too much content', 'Overwhelming pace'],
    { dominant: 'SADNESS', dominantPercent: 31.8 }, 0.63,
    'Repeatedly discussed negative theme: 47% negative across 71 mentions'),
  topic('setup', 'Setup problems', 43, 'PAIN_POINT', [13, 3, 27],
    ['Setup problems', 'Environment issues'],
    { dominant: 'ANGER', dominantPercent: 34.2 }, 0.61,
    'Repeatedly discussed negative theme: 63% negative across 43 mentions'),
  topic('complexity', 'React complexity', 184, 'MIXED', [79, 39, 66],
    ['React complexity', 'Too many concepts'],
    { dominant: 'TRUST', dominantPercent: 29.7 }, 0.68,
    'Mixed discussion: 43% positive, 36% negative across 184 mentions'),
];

const DEMO_TOPIC_ANALYSIS: TopicAnalysis = {
  videoId: VIDEO.video.videoId,
  status: 'READY',
  analyzedComments: ANALYZED,
  message: null,
  topics: DEMO_TOPICS,
  mostDiscussed: DEMO_TOPICS.filter((t) => t.category === 'MOST_DISCUSSED'),
  mostAppreciated: DEMO_TOPICS.filter((t) => t.category === 'APPRECIATED'),
  painPoints: DEMO_TOPICS.filter((t) => t.category === 'PAIN_POINT'),
  mixedTopics: DEMO_TOPICS.filter((t) => t.category === 'MIXED'),
};

const topicsService: TopicsService = {
  getTopics(_request: TopicsServiceRequest): Promise<TopicsServiceResult> {
    if (scene === 'topics-error') {
      return Promise.resolve({
        kind: 'error',
        code: 'network_error',
        message: 'Backend is unreachable. Start the local backend (see README) and try again.',
      });
    }
    if (scene === 'topics-loading') {
      return new Promise<TopicsServiceResult>(() => undefined);
    }
    if (scene === 'topics-empty') {
      return Promise.resolve({
        kind: 'success',
        data: {
          ...DEMO_TOPIC_ANALYSIS,
          topics: [],
          mostDiscussed: [],
          mostAppreciated: [],
          painPoints: [],
          mixedTopics: [],
          message: 'Not enough repeated discussion yet.',
        },
      });
    }
    return Promise.resolve({ kind: 'success', data: DEMO_TOPIC_ANALYSIS });
  },
};

// ---------------------------------------------------------------------------
// Sprint 7 §32: demo audience insight (CLEARLY SYNTHETIC test data) shaped
// exactly like GET /api/videos/{id}/insight - same evidence numbers as the
// demo sentiment/topic sets above, deterministic source (the default).
// ---------------------------------------------------------------------------

const DEMO_INSIGHT: InsightAnalysis = {
  videoId: VIDEO.video.videoId,
  status: 'READY',
  message: null,
  headline: 'The audience response is strongly positive',
  summary:
    'The analyzed audience shows 68.3% positive, 29.0% neutral, 2.7% negative across 1,211 analyzed comments. Viewers repeatedly praised Clear explanations (284 mentions, 91.0% positive). Most discussion centers on Learning roadmap (312 mentions), while Learning difficulty is a recurring concern (96 mentions, 58.0% negative).',
  cards: [
    {
      category: 'OVERALL_REACTION',
      title: 'The audience response is strongly positive',
      body:
        'The analyzed audience shows 68.3% positive, 29.0% neutral, 2.7% negative across 1,211 analyzed comments.',
      evidence: [
        { kind: 'SENTIMENT', label: 'Overall sentiment', value: '68.3% positive', detail: '29.0% neutral · 2.7% negative', topicId: null },
        { kind: 'EMOTION', label: 'Dominant emotion', value: 'TRUST 37.7%', detail: null, topicId: null },
        { kind: 'MIXED', label: 'Divided discussion', value: 'React complexity · 184 mentions', detail: null, topicId: 'complexity' },
        { kind: 'SAMPLE', label: 'Evidence base', value: '1211 analyzed comments', detail: '2429 collected · 1218 skipped', topicId: null },
      ],
    },
    {
      category: 'WHAT_WORKED',
      title: 'Clear explanations',
      body: 'Viewers repeatedly praised Clear explanations (284 mentions, 91.0% positive).',
      evidence: [
        { kind: 'APPRECIATED', label: 'Clear explanations', value: '284 mentions', detail: '91.0% positive', topicId: 'explanations' },
        { kind: 'SENTIMENT', label: 'Overall sentiment', value: '68.3% positive', detail: '29.0% neutral · 2.7% negative', topicId: null },
      ],
    },
    {
      category: 'MAIN_DISCUSSION',
      title: 'Learning roadmap',
      body: 'Learning roadmap is the main discussion theme (312 mentions, 25.8% of analyzed comments).',
      evidence: [
        { kind: 'TOPIC', label: 'Learning roadmap', value: '312 mentions', detail: '25.8% of analyzed', topicId: 'roadmap' },
        { kind: 'MIXED', label: 'React complexity', value: '184 mentions', detail: '42.9% positive / 35.9% negative', topicId: 'complexity' },
      ],
    },
    {
      category: 'PAIN_POINT',
      title: 'Learning difficulty',
      body: 'Some viewers expressed recurring frustration with Learning difficulty (96 mentions, 58.0% negative).',
      evidence: [
        { kind: 'PAIN_POINT', label: 'Learning difficulty', value: '96 mentions', detail: '58.0% negative', topicId: 'difficulty' },
        { kind: 'SENTIMENT', label: 'Overall sentiment', value: '68.3% positive', detail: '29.0% neutral · 2.7% negative', topicId: null },
      ],
    },
    {
      category: 'EMOTIONAL_SIGNAL',
      title: 'TRUST',
      body: 'TRUST is the dominant detected emotion (37.7% of analyzed comments), associated with the dominant positive sentiment.',
      evidence: [
        { kind: 'EMOTION', label: 'TRUST', value: '37.7% of analyzed comments', detail: null, topicId: null },
        { kind: 'INTENSITY', label: 'Overall intensity', value: 'LOW', detail: null, topicId: null },
        { kind: 'CONFIDENCE', label: 'Sentiment confidence', value: '0.71', detail: null, topicId: null },
      ],
    },
    {
      category: 'TAKEAWAY',
      title: 'Key takeaway',
      body: 'Overall, the audience response is strongly positive, with the strongest appreciation centered on Clear explanations. Learning difficulty remains a recurring concern.',
      evidence: [
        { kind: 'SENTIMENT', label: 'Overall sentiment', value: '68.3% positive', detail: '29.0% neutral · 2.7% negative', topicId: null },
        { kind: 'APPRECIATED', label: 'Clear explanations', value: '284 mentions', detail: '91.0% positive', topicId: 'explanations' },
        { kind: 'PAIN_POINT', label: 'Learning difficulty', value: '96 mentions', detail: '58.0% negative', topicId: 'difficulty' },
      ],
    },
  ],
  sample: { collected: 2429, analyzed: ANALYZED, skipped: 1218 },
  source: 'deterministic',
  provider: { name: 'deterministic', model: null },
  evidenceVersion: '1211:demo',
  generatedAt: '2026-10-01T12:00:00Z',
  generationMs: 4,
};

const insightService: InsightService = {
  getInsight(_request: InsightServiceRequest): Promise<InsightServiceResult> {
    if (scene === 'insight-error') {
      return Promise.resolve({
        kind: 'error',
        code: 'network_error',
        message: 'Backend is unreachable. Start the local backend (see README) and try again.',
      });
    }
    if (scene === 'insight-loading') {
      return new Promise<InsightServiceResult>(() => undefined);
    }
    if (scene === 'insight-empty') {
      return Promise.resolve({
        kind: 'success',
        data: {
          ...DEMO_INSIGHT,
          status: 'INSUFFICIENT_DATA',
          message: 'Not enough analyzed audience evidence to generate a reliable insight.',
          headline: '',
          summary: '',
          cards: [],
        },
      });
    }
    if (scene === 'insight-llm') {
      return Promise.resolve({
        kind: 'success',
        data: {
          ...DEMO_INSIGHT,
          source: 'llm',
          provider: { name: 'openai_compatible', model: 'demo-model' },
          generationMs: 640,
        },
      });
    }
    return Promise.resolve({ kind: 'success', data: DEMO_INSIGHT });
  },
};

// ---------------------------------------------------------------------------
// Sprint 8: demo realtime status (CLEARLY SYNTHETIC test data) shaped exactly
// like GET /api/videos/{id}/realtime. Version stays CONSTANT so the strip's
// silent-refetch path is not exercised in visual QA (it is covered by tests).
// ---------------------------------------------------------------------------

const DEMO_REALTIME: RealtimeStatus = {
  videoId: 'vz1RlUyrc3w',
  enabled: true,
  monitoring: true,
  pollIntervalSeconds: 30,
  lastCheckedAt: new Date(Date.now() - 8_000).toISOString(),
  lastUpdatedAt: new Date(Date.now() - 95_000).toISOString(),
  newComments: 22,
  totalComments: 2429,
  analyzed: ANALYZED,
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
};

const realtimeService: RealtimeService = {
  getRealtime(_request: RealtimeServiceRequest): Promise<RealtimeServiceResult> {
    if (scene === 'realtime-error') {
      return Promise.resolve({
        kind: 'error',
        code: 'network_error',
        message: 'Backend is unreachable. Start the local backend (see README) and try again.',
      });
    }
    if (scene === 'realtime-standby') {
      return Promise.resolve({
        kind: 'success',
        data: { ...DEMO_REALTIME, monitoring: false },
      });
    }
    if (scene === 'realtime-off') {
      return Promise.resolve({
        kind: 'success',
        data: { ...DEMO_REALTIME, enabled: false, monitoring: false },
      });
    }
    if (scene === 'realtime-falling') {
      return Promise.resolve({
        kind: 'success',
        data: {
          ...DEMO_REALTIME,
          newComments: 47,
          pending: 12,
          analyzed: ANALYZED - 12,
          version: '2476:1199',
          trend: {
            state: 'FALLING',
            changePp: -6.8,
            positivePp: -5.4,
            neutralPp: -1.4,
            negativePp: 6.8,
          },
          activity: { level: 'HIGH', newRecent: 47, windowMinutes: 1, ratePerMinute: 7.9 },
        },
      });
    }
    return Promise.resolve({ kind: 'success', data: DEMO_REALTIME });
  },
};

// Aged context → page scrolls are eligible to minimize (production behavior).
// Set AFTER mount so the store's auto-open/auto-analysis path runs exactly
// like it does when YouTube SPA navigation delivers a context.
const store = createStore();

// Real mount path: zero-size fixed host (YouTube stays clickable) + Shadow DOM
// + the compiled overlay stylesheet, exactly like src/content/index.ts.
const host = document.createElement('div');
host.id = 'sentiment-ai-extension-root';
host.style.position = 'fixed';
host.style.top = '0';
host.style.left = '0';
host.style.width = '0';
host.style.height = '0';
host.style.zIndex = '2147483647';
host.style.pointerEvents = 'none';

const shadow = host.attachShadow({ mode: 'open' });
const style = document.createElement('style');
style.textContent = overlayCss;
const root = document.createElement('div');
root.className = 'sai-root';
shadow.append(style, root);
document.body.appendChild(host);

mountApp(
  root,
  store,
  analysisService,
  sentimentService,
  undefined,
  topicsService,
  insightService,
  realtimeService,
);

store.setVideoContext(
  parseVideoContext(
    'https://www.youtube.com/watch?v=vz1RlUyrc3w',
    Date.now() - 60_000,
  ),
);
