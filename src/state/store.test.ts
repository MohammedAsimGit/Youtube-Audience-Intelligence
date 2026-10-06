import { describe, expect, it } from 'vitest';
import { parseVideoContext } from '../services/youtube/video-context';
import type {
  AnalysisJob,
  SentimentAnalysis,
  VideoDataResponse,
} from '../shared/types';
import { createStore } from './store';

const ACQUIRED_FIXTURE: VideoDataResponse = {
  video: {
    videoId: 'AAAAAAAAAAA',
    title: 'Fixture Title',
    description: null,
    channelId: 'UC123',
    channelTitle: 'Fixture Channel',
    publishedAt: '2024-03-01T10:00:00Z',
    categoryId: '28',
    duration: null,
    statistics: { viewCount: 100, likeCount: 5, commentCount: 42 },
  },
  comments: { items: [], count: 42, hasMore: false, status: 'ok' },
  source: { provider: 'youtube', retrievedAt: '2024-03-04T00:00:00Z', cached: false },
};

describe('store transitions', () => {
  it('starts closed', () => {
    const store = createStore();
    expect(store.getState().status).toBe('closed');
    expect(store.getState().minimized).toBe(false);
  });

  it('open → ready lifecycle keeps status until explicitly changed', () => {
    const store = createStore();
    store.open();
    expect(store.getState().status).toBe('open');
    store.setStatus('ready');
    expect(store.getState().status).toBe('ready');
  });

  it('minimize preserves status, restore brings it back', () => {
    const store = createStore();
    store.open();
    store.setStatus('ready');
    store.minimize();
    expect(store.getState().minimized).toBe(true);
    expect(store.getState().status).toBe('ready');
    store.restore();
    expect(store.getState().minimized).toBe(false);
    expect(store.getState().status).toBe('ready');
  });

  it('close resets to closed and clears notice', () => {
    const store = createStore();
    store.open();
    store.setNotice('hello');
    store.close();
    expect(store.getState().status).toBe('closed');
    expect(store.getState().notice).toBeNull();
  });

  it('supports future analysis states without model changes', () => {
    const store = createStore();
    store.setStatus('analyzing');
    expect(store.getState().status).toBe('analyzing');
    store.setStatus('complete');
    expect(store.getState().status).toBe('complete');
    store.setStatus('error');
    expect(store.getState().status).toBe('error');
  });

  it('updates video context (video A → video B) and notifies subscribers', () => {
    const store = createStore();
    let notifications = 0;
    store.subscribe(() => {
      notifications += 1;
    });

    const videoA = parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA');
    store.setVideoContext(videoA);
    expect(store.getState().videoContext?.videoId).toBe('AAAAAAAAAAA');

    const videoB = parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB');
    store.setVideoContext(videoB);
    expect(store.getState().videoContext?.videoId).toBe('BBBBBBBBBBB');
    expect(notifications).toBe(2);
  });
});

describe('acquisition lifecycle (Sprint 2)', () => {
  it('starts idle', () => {
    const store = createStore();
    expect(store.getState().acquisition.status).toBe('idle');
    expect(store.getState().acquisition.data).toBeNull();
  });

  it('begin → complete carries real data', () => {
    const store = createStore();
    store.beginAcquisition('AAAAAAAAAAA');
    expect(store.getState().acquisition.status).toBe('loading');

    store.completeAcquisition('AAAAAAAAAAA', ACQUIRED_FIXTURE);
    const acquisition = store.getState().acquisition;
    expect(acquisition.status).toBe('success');
    expect(acquisition.data?.video.title).toBe('Fixture Title');
    expect(acquisition.errorCode).toBeNull();
  });

  it('begin → fail carries code and friendly message', () => {
    const store = createStore();
    store.beginAcquisition('AAAAAAAAAAA');
    store.failAcquisition('AAAAAAAAAAA', 'quota_exceeded', 'Quota exhausted.');
    const acquisition = store.getState().acquisition;
    expect(acquisition.status).toBe('error');
    expect(acquisition.errorCode).toBe('quota_exceeded');
    expect(acquisition.errorMessage).toBe('Quota exhausted.');
    expect(acquisition.data).toBeNull();
  });

  it('clearAcquisition resets to idle', () => {
    const store = createStore();
    store.beginAcquisition('AAAAAAAAAAA');
    store.clearAcquisition();
    expect(store.getState().acquisition.status).toBe('idle');
  });

  it('changing video clears stale data and auto-opens the overlay for the new video', () => {
    const store = createStore();
    store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'));
    store.open();
    store.setStatus('complete');
    store.completeAcquisition('AAAAAAAAAAA', ACQUIRED_FIXTURE);
    store.setNotice('old notice');
    expect(store.getState().status).toBe('complete');

    store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'));
    const state = store.getState();
    expect(state.acquisition.status).toBe('idle');
    expect(state.acquisition.data).toBeNull();
    expect(state.notice).toBeNull();
    // New video active → overlay auto-opens READY (no auto-analysis).
    expect(state.status).toBe('ready');
    expect(state.minimized).toBe(false);
    expect(state.videoContext?.videoId).toBe('BBBBBBBBBBB');
  });

  it('video change reopens a minimized overlay fully for the new video', () => {
    const store = createStore();
    store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'));
    store.open();
    store.setStatus('ready');
    store.minimize();
    expect(store.getState().minimized).toBe(true);

    store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'));
    expect(store.getState().status).toBe('ready');
    expect(store.getState().minimized).toBe(false);
  });

  it('initial detection of a video auto-opens for zero-click analysis (Sprint 4.4)', () => {
    const store = createStore();
    store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'));
    // §4: a video present at boot IS a detected video - the overlay opens
    // so the automatic flow can start without any user interaction.
    expect(store.getState().status).toBe('ready');
    expect(store.getState().videoContext?.videoId).toBe('AAAAAAAAAAA');
    expect(store.getState().acquisition.status).toBe('idle'); // flow starts it
    // Same-context refresh keeps the same overlay state (no re-trigger, §24).
    store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'));
    expect(store.getState().status).toBe('ready');
  });

  it('initial detection on a non-video page stays closed (FAB only)', () => {
    const store = createStore();
    store.setVideoContext(parseVideoContext('https://www.youtube.com/'));
    expect(store.getState().status).toBe('closed');
    expect(store.getState().videoContext?.videoId).toBeNull();
  });

  it('auto-opens when a video becomes active from a non-video page', () => {
    const store = createStore({
      videoContext: parseVideoContext('https://www.youtube.com/'),
    });
    expect(store.getState().status).toBe('closed');

    store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'));
    expect(store.getState().status).toBe('ready'); // open, but NOT analyzing
    expect(store.getState().acquisition.status).toBe('idle');
    expect(store.getState().minimized).toBe(false);
  });

  it('leaving the video context (search page) closes the overlay', () => {
    const store = createStore();
    store.setVideoContext(parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'));
    store.open();
    store.setStatus('ready');
    store.completeAcquisition('AAAAAAAAAAA', ACQUIRED_FIXTURE);
    expect(store.getState().status).toBe('ready');

    store.setVideoContext(
      parseVideoContext('https://www.youtube.com/results?search_query=cats'),
    );
    const state = store.getState();
    expect(state.status).toBe('closed');
    expect(state.minimized).toBe(false);
    expect(state.acquisition.status).toBe('idle');
    expect(state.videoContext?.videoId).toBeNull();
  });

  it('same-video context refresh does not wipe acquisition', () => {
    const store = createStore();
    const url = 'https://www.youtube.com/watch?v=AAAAAAAAAAA';
    store.setVideoContext(parseVideoContext(url));
    store.beginAcquisition('AAAAAAAAAAA');
    store.completeAcquisition('AAAAAAAAAAA', ACQUIRED_FIXTURE);
    // SPA may re-emit the same URL (e.g. popstate) - data stays.
    store.setVideoContext(parseVideoContext(url));
    expect(store.getState().acquisition.status).toBe('success');
  });
});

describe('sentiment lifecycle (Sprint 4)', () => {
  const SENTIMENT_FIXTURE: SentimentAnalysis = {
    videoId: 'AAAAAAAAAAA',
    status: 'PROCESSED',
    stats: {
      totalComments: 10,
      analyzed: 10,
      skipped: 0,
      positive: 5,
      neutral: 3,
      negative: 2,
      positivePercent: 50,
      neutralPercent: 30,
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

  function storeOnVideoA() {
    const store = createStore();
    store.setVideoContext(
      parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'),
    );
    return store;
  }

  it('starts idle', () => {
    const store = storeOnVideoA();
    expect(store.getState().sentiment.status).toBe('idle');
    expect(store.getState().sentiment.data).toBeNull();
  });

  it('begin → complete carries backend aggregates', () => {
    const store = storeOnVideoA();
    store.beginSentiment('AAAAAAAAAAA');
    expect(store.getState().sentiment.status).toBe('loading');

    store.completeSentiment('AAAAAAAAAAA', SENTIMENT_FIXTURE);
    const sentiment = store.getState().sentiment;
    expect(sentiment.status).toBe('success');
    expect(sentiment.videoId).toBe('AAAAAAAAAAA');
    expect(sentiment.data?.dominantSentiment).toBe('POSITIVE');
    expect(sentiment.data?.stats.positivePercent).toBe(50);
    expect(sentiment.errorCode).toBeNull();
  });

  it('begin → fail carries code and friendly message without data', () => {
    const store = storeOnVideoA();
    store.beginSentiment('AAAAAAAAAAA');
    store.failSentiment('AAAAAAAAAAA', 'storage_unavailable', 'Store unavailable.');
    const sentiment = store.getState().sentiment;
    expect(sentiment.status).toBe('error');
    expect(sentiment.errorCode).toBe('storage_unavailable');
    expect(sentiment.errorMessage).toBe('Store unavailable.');
    expect(sentiment.data).toBeNull();
  });

  it('clearSentiment resets to idle', () => {
    const store = storeOnVideoA();
    store.beginSentiment('AAAAAAAAAAA');
    store.clearSentiment();
    expect(store.getState().sentiment.status).toBe('idle');
  });

  it('store-side stale guard discards results for a non-active video', () => {
    const store = storeOnVideoA(); // active video is A
    store.beginSentiment('AAAAAAAAAAA');
    // Video B's result must be ignored even if a caller forgets its own guard.
    store.completeSentiment('BBBBBBBBBBB', {
      ...SENTIMENT_FIXTURE,
      videoId: 'BBBBBBBBBBB',
    });
    expect(store.getState().sentiment.status).toBe('loading');
    expect(store.getState().sentiment.data).toBeNull();

    store.failSentiment('BBBBBBBBBBB', 'network_error', 'offline');
    expect(store.getState().sentiment.status).toBe('loading');
  });

  it('video change clears stale sentiment for the next video', () => {
    const store = storeOnVideoA();
    store.beginSentiment('AAAAAAAAAAA');
    store.completeSentiment('AAAAAAAAAAA', SENTIMENT_FIXTURE);
    expect(store.getState().sentiment.status).toBe('success');

    store.setVideoContext(
      parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
    );
    expect(store.getState().sentiment.status).toBe('idle');
    expect(store.getState().sentiment.data).toBeNull();
    expect(store.getState().sentiment.videoId).toBeNull();
  });

  it('same-video context refresh keeps sentiment results', () => {
    const store = storeOnVideoA();
    store.beginSentiment('AAAAAAAAAAA');
    store.completeSentiment('AAAAAAAAAAA', SENTIMENT_FIXTURE);
    store.setVideoContext(
      parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'),
    );
    expect(store.getState().sentiment.status).toBe('success');
  });
});

describe('background analysis job (Sprint 4.3)', () => {
  const VIDEO_A = 'AAAAAAAAAAA';

  function jobFixture(overrides?: Partial<AnalysisJob>): AnalysisJob {
    return {
      jobId: 'job-1',
      videoId: VIDEO_A,
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

  function storeOnVideoA() {
    return createStore({
      videoContext: parseVideoContext(
        'https://www.youtube.com/watch?v=AAAAAAAAAAA',
      ),
    });
  }

  it('beginJob starts running with no fabricated counts', () => {
    const store = storeOnVideoA();
    store.beginJob(VIDEO_A, 'job-1');
    const job = store.getState().job;
    expect(job.status).toBe('running');
    expect(job.jobId).toBe('job-1');
    expect(job.videoId).toBe(VIDEO_A);
    expect(job.job).toBeNull(); // counts arrive only from real polls (§7)
  });

  it('updateJob keeps the latest snapshot', () => {
    const store = storeOnVideoA();
    store.beginJob(VIDEO_A, 'job-1');
    store.updateJob(VIDEO_A, jobFixture({ collected: 1842 }));
    store.updateJob(
      VIDEO_A,
      jobFixture({ status: 'ANALYZING', phase: 'SENTIMENT', analyzed: 1200, pending: 642 }),
    );
    const job = store.getState().job;
    expect(job.job?.analyzed).toBe(1200);
    expect(job.job?.pending).toBe(642);
  });

  it('store-side stale guard discards snapshots for a non-active video', () => {
    const store = storeOnVideoA();
    store.beginJob(VIDEO_A, 'job-1');
    store.updateJob('BBBBBBBBBBB', jobFixture({ videoId: 'BBBBBBBBBBB', collected: 9 }));
    expect(store.getState().job.job).toBeNull();

    store.failJob('BBBBBBBBBBB', 'network_error', 'offline');
    expect(store.getState().job.status).toBe('running');
  });

  it('discards snapshots from a superseded job id (A → B → A)', () => {
    const store = storeOnVideoA();
    store.beginJob(VIDEO_A, 'job-2'); // second run for the same video
    store.updateJob(VIDEO_A, jobFixture({ jobId: 'job-1', collected: 999 }));
    expect(store.getState().job.job).toBeNull(); // job-1 snapshot dropped

    store.updateJob(VIDEO_A, jobFixture({ jobId: 'job-2', collected: 10 }));
    expect(store.getState().job.job?.collected).toBe(10);
  });

  it('failJob keeps the last snapshot so counts stay real', () => {
    const store = storeOnVideoA();
    store.beginJob(VIDEO_A, 'job-1');
    store.updateJob(VIDEO_A, jobFixture({ collected: 1842, status: 'FAILED' }));
    store.failJob(VIDEO_A, 'upstream_timeout', 'The request timed out.');
    const job = store.getState().job;
    expect(job.status).toBe('error');
    expect(job.errorCode).toBe('upstream_timeout');
    expect(job.errorMessage).toBe('The request timed out.');
    expect(job.job?.collected).toBe(1842); // real count for §25 FAILED
  });

  it('clearJob resets to idle', () => {
    const store = storeOnVideoA();
    store.beginJob(VIDEO_A, 'job-1');
    store.clearJob();
    expect(store.getState().job.status).toBe('idle');
    expect(store.getState().job.jobId).toBeNull();
  });

  it('video change clears the job slice for the next video', () => {
    const store = storeOnVideoA();
    store.beginJob(VIDEO_A, 'job-1');
    store.updateJob(VIDEO_A, jobFixture({ collected: 500 }));

    store.setVideoContext(
      parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
    );
    const job = store.getState().job;
    expect(job.status).toBe('idle');
    expect(job.jobId).toBeNull();
    expect(job.job).toBeNull();
  });
});

describe('active console section (UI-only, multi-section redesign)', () => {
  function storeOnVideoA() {
    const store = createStore();
    store.setVideoContext(
      parseVideoContext('https://www.youtube.com/watch?v=AAAAAAAAAAA'),
    );
    return store;
  }

  it('defaults to overview and switches with setSection', () => {
    const store = createStore();
    expect(store.getState().activeSection).toBe('overview');
    store.setSection('emotion');
    expect(store.getState().activeSection).toBe('emotion');
    store.setSection('topics');
    expect(store.getState().activeSection).toBe('topics');
  });

  it('survives minimize → restore (minimize is a visibility flag only)', () => {
    const store = storeOnVideoA();
    store.setSection('insight');
    store.minimize();
    expect(store.getState().activeSection).toBe('insight');
    store.restore();
    expect(store.getState().activeSection).toBe('insight');
  });

  it('resets to overview whenever the active video changes', () => {
    const store = storeOnVideoA();
    store.setSection('sentiment');
    store.setVideoContext(
      parseVideoContext('https://www.youtube.com/watch?v=BBBBBBBBBBB'),
    );
    expect(store.getState().activeSection).toBe('overview');

    store.setSection('topics');
    store.setVideoContext(null);
    expect(store.getState().activeSection).toBe('overview');
  });
});
