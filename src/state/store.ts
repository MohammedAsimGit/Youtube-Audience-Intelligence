import { logger } from '../lib/logger';
import type {
  AcquisitionErrorCode,
  AcquisitionState,
  AnalysisJob,
  JobState,
  OverlayState,
  OverlayStatus,
  InsightAnalysis,
  InsightState,
  RealtimeState,
  RealtimeStatus,
  SectionId,
  SentimentAnalysis,
  SentimentState,
  TopicAnalysis,
  TopicsState,
  VideoContext,
  VideoDataResponse,
} from '../shared/types';

const IDLE_ACQUISITION: AcquisitionState = {
  status: 'idle',
  videoId: null,
  data: null,
  errorCode: null,
  errorMessage: null,
};

const IDLE_SENTIMENT: SentimentState = {
  status: 'idle',
  videoId: null,
  data: null,
  errorCode: null,
  errorMessage: null,
};

const IDLE_TOPICS: TopicsState = {
  status: 'idle',
  videoId: null,
  data: null,
  errorCode: null,
  errorMessage: null,
};

const IDLE_INSIGHT: InsightState = {
  status: 'idle',
  videoId: null,
  data: null,
  errorCode: null,
  errorMessage: null,
};

const IDLE_JOB: JobState = {
  status: 'idle',
  videoId: null,
  jobId: null,
  job: null,
  errorCode: null,
  errorMessage: null,
};

const IDLE_REALTIME: RealtimeState = {
  status: 'idle',
  videoId: null,
  data: null,
  errorCode: null,
  errorMessage: null,
};

const INITIAL_STATE: OverlayState = {
  status: 'closed',
  minimized: false,
  activeSection: 'overview',
  videoContext: null,
  notice: null,
  acquisition: IDLE_ACQUISITION,
  sentiment: IDLE_SENTIMENT,
  topics: IDLE_TOPICS,
  insight: IDLE_INSIGHT,
  job: IDLE_JOB,
  realtime: IDLE_REALTIME,
};

export interface Store {
  getState(): OverlayState;
  subscribe(listener: () => void): () => void;
  /** CLOSED → OPEN (shell mounting). */
  open(): void;
  /** Any state → CLOSED. FAB stays alive; reopening starts fresh. */
  close(): void;
  /** Preserve status, collapse to compact panel. */
  minimize(): void;
  restore(): void;
  /**
   * UI-only console section switch (overview/sentiment/emotion/topics/
   * insight). Pure presentation: no fetch, no refetch, no data change -
   * realtime updates and background jobs never call this, so an in-flight
   * update can never move the user off their active section.
   */
  setSection(section: SectionId): void;
  setStatus(status: OverlayStatus): void;
  setNotice(notice: string | null): void;
  /**
   * Old context → detect change → new context → UI reacts.
   * - Detection with an active video id (content-script boot OR SPA
   *   navigation): store the context, auto-open READY for that video and
   *   clear stale slices so Video A's data can never render for Video B.
   *   Sprint 4.4 (§4): this IS the analysis trigger - the overlay's
   *   automatic flow starts the job without any user click.
   * - Same video id: context refresh only (§24) - overlay untouched, no
   *   new job, no dataset reset.
   * - Leaving the video context (home/search): overlay closes; FAB stays.
   */
  setVideoContext(context: VideoContext | null): void;
  /** READY → LOADING for one video. */
  beginAcquisition(videoId: string): void;
  /** LOADING → SUCCESS with real acquired data. */
  completeAcquisition(videoId: string, data: VideoDataResponse): void;
  /** LOADING → ERROR with a friendly, stack-free message. */
  failAcquisition(
    videoId: string,
    errorCode: AcquisitionErrorCode,
    errorMessage: string,
  ): void;
  /** Reset acquisition to idle (retry / honest-unavailable path). */
  clearAcquisition(): void;
  /**
   * Sprint 4 sentiment lifecycle. Mirror of the acquisition pattern with an
   * extra store-side identity guard: every begin/complete/fail call names its
   * video and is IGNORED unless that video is still active - so a result
   * that lands after navigation can never leak into the next video's state
   * even if the caller forgets its own stale check.
   */
  /** SENTIMENT: idle → loading for one video. */
  beginSentiment(videoId: string): void;
  /** SENTIMENT: loading → success with backend aggregates. */
  completeSentiment(videoId: string, data: SentimentAnalysis): void;
  /** SENTIMENT: loading → error with a friendly, stack-free message. */
  failSentiment(
    videoId: string,
    errorCode: AcquisitionErrorCode,
    errorMessage: string,
  ): void;
  /** Reset sentiment to idle (retry / video change / honest unavailable). */
  clearSentiment(): void;
  /**
   * Sprint 6 topic lifecycle (§40 stale protection). Same store-side
   * identity guard as sentiment: every begin/complete/fail call names its
   * video and is IGNORED unless that video is still active - a topic
   * response that lands after navigation can never leak into the next
   * video's overlay.
   */
  /** TOPICS: idle → loading for one video. */
  beginTopics(videoId: string): void;
  /** TOPICS: loading → success with backend discovery results. */
  completeTopics(videoId: string, data: TopicAnalysis): void;
  /** TOPICS: loading → error (sentiment stays intact, §39). */
  failTopics(
    videoId: string,
    errorCode: AcquisitionErrorCode,
    errorMessage: string,
  ): void;
  /** Reset topics to idle (retry / video change / re-analysis). */
  clearTopics(): void;
  /**
   * Sprint 7 audience-insight lifecycle (§26 stale protection). Same
   * store-side identity guard as sentiment/topics: every begin/complete/
   * fail call names its video and is IGNORED unless that video is still
   * active - an insight response that lands after navigation can never
   * leak into the next video's overlay.
   */
  /** INSIGHT: idle → loading for one video. */
  beginInsight(videoId: string): void;
  /** INSIGHT: loading → success with backend-generated insight. */
  completeInsight(videoId: string, data: InsightAnalysis): void;
  /** INSIGHT: loading → error (sentiment/topics stay intact, §30). */
  failInsight(
    videoId: string,
    errorCode: AcquisitionErrorCode,
    errorMessage: string,
  ): void;
  /** Reset insight to idle (retry / video change / re-analysis). */
  clearInsight(): void;
  /**
   * Sprint 4.3 background job lifecycle. Same store-side identity guard as
   * sentiment: every call names its video and is IGNORED unless that video
   * is still active - plus updateJob also requires the job id to match, so
   * a late snapshot from a superseded job can never reach the overlay.
   */
  /** POST accepted -> track the running job for one video. */
  beginJob(videoId: string, jobId: string): void;
  /** Latest polled snapshot (progress + terminal state) for this job. */
  updateJob(videoId: string, job: AnalysisJob): void;
  /** Terminal failure / transport error; keeps the last snapshot for counts. */
  failJob(
    videoId: string,
    errorCode: AcquisitionErrorCode,
    errorMessage: string,
  ): void;
  /** Reset the job slice (retry / video change). */
  clearJob(): void;
  /**
   * Sprint 8 realtime monitor status. Same store-side identity guard as
   * sentiment/topics/insight/job: every call names its video and is
   * IGNORED unless that video is still active - a poll that lands after
   * navigation can never leak into the next video's strip (§40).
   */
  /** Latest /realtime snapshot for this video (silent poll, no spinner). */
  updateRealtime(videoId: string, data: RealtimeStatus): void;
  /** Poll failed; keeps the last known status so the strip never blanks. */
  failRealtime(
    videoId: string,
    errorCode: AcquisitionErrorCode,
    errorMessage: string,
  ): void;
  /** Reset the realtime slice (video change / overlay restart). */
  clearRealtime(): void;
}

/**
 * Small external store consumed by React via useSyncExternalStore. Chosen over
 * context/reducer so the content script (navigation detector) can push video
 * context changes into application state without touching the UI tree - the
 * required separation: Detection → Video Context → Application State → React UI.
 */
export function createStore(initial: Partial<OverlayState> = {}): Store {
  let state: OverlayState = { ...INITIAL_STATE, ...initial };
  const listeners = new Set<() => void>();

  const update = (reason: string, patch: Partial<OverlayState>): void => {
    const previous = state;
    const next: OverlayState = { ...previous, ...patch };
    const changed =
      previous.status !== next.status ||
      previous.minimized !== next.minimized ||
      previous.activeSection !== next.activeSection ||
      previous.videoContext !== next.videoContext ||
      previous.notice !== next.notice ||
      previous.acquisition !== next.acquisition ||
    previous.sentiment !== next.sentiment ||
    previous.topics !== next.topics ||
    previous.insight !== next.insight ||
    previous.job !== next.job ||
      previous.realtime !== next.realtime;
    if (!changed) return;
    state = next;
    logger.debug(`state (${reason})`, {
      status: `${previous.status} -> ${next.status}`,
      minimized: `${previous.minimized} -> ${next.minimized}`,
      videoId: next.videoContext?.videoId ?? null,
      acquisition: next.acquisition.status,
      sentiment: next.sentiment.status,
    });
    for (const listener of listeners) listener();
  };

  const acquisitionPatch = (acquisition: AcquisitionState): Partial<OverlayState> => ({
    acquisition,
  });

  const sentimentPatch = (sentiment: SentimentState): Partial<OverlayState> => ({
    sentiment,
  });

  const topicsPatch = (topics: TopicsState): Partial<OverlayState> => ({ topics });

  const insightPatch = (insight: InsightState): Partial<OverlayState> => ({ insight });

  const jobPatch = (job: JobState): Partial<OverlayState> => ({ job });

  const realtimePatch = (realtime: RealtimeState): Partial<OverlayState> => ({
    realtime,
  });

  /** Store-side stale guard: the call's video must still be the active one. */
  const isCurrentVideo = (videoId: string): boolean =>
    (state.videoContext?.videoId ?? null) === videoId;

  return {
    getState: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    open: () => update('open', { status: 'open', minimized: false, notice: null }),
    close: () => update('close', { status: 'closed', minimized: false, notice: null }),
    minimize: () => update('minimize', { minimized: true }),
    restore: () => update('restore', { minimized: false }),
    setSection: (activeSection) => update('section', { activeSection }),
    setStatus: (status) => update(`status:${status}`, { status }),
    setNotice: (notice) => update('notice', { notice }),
    setVideoContext: (videoContext) => {
      const previous = state.videoContext;
      const previousId = previous?.videoId ?? null;
      const nextId = videoContext?.videoId ?? null;

      // Same active video (e.g. playlist params added): context refresh;
      // overlay untouched - YouTube's duplicate DOM/navigation events for
      // one video can never restart an analysis (§7/§24).
      if (previous !== null && previousId === nextId) {
        update('video-context', { videoContext });
        return;
      }

      // Video lifecycle (FR-11): identity is the video id. Drop stale
      // acquisition + notice + loading/error state for the new context so
      // Video A's data can never render for Video B (a transient null id
      // mid-navigation behaves the same; detection resolves the final id).
      if (nextId !== null) {
        // A video became active (boot detection or SPA navigation) -> the
        // overlay auto-opens READY for it. Sprint 4.4: the overlay's
        // automatic flow takes it from there (no Analyze click). The job
        // slice resets too - Video A's polling can never render under
        // Video B (Sprint 4.3 §27).
        update('video-context+open', {
          videoContext,
          acquisition: IDLE_ACQUISITION,
          sentiment: IDLE_SENTIMENT,
          topics: IDLE_TOPICS,
          insight: IDLE_INSIGHT,
          job: IDLE_JOB,
          realtime: IDLE_REALTIME,
          notice: null,
          status: 'ready',
          minimized: false,
          // Section content is per-video: a new video always opens on the
          // default Overview section (never stale section content).
          activeSection: 'overview',
        });
      } else {
        // Left the video context (home/search/channel): close; FAB stays.
        update('video-context+reset', {
          videoContext,
          acquisition: IDLE_ACQUISITION,
          sentiment: IDLE_SENTIMENT,
          topics: IDLE_TOPICS,
          insight: IDLE_INSIGHT,
          job: IDLE_JOB,
          realtime: IDLE_REALTIME,
          notice: null,
          status: 'closed',
          minimized: false,
          activeSection: 'overview',
        });
      }
    },
    beginAcquisition: (videoId) =>
      update(
        'acquisition:loading',
        acquisitionPatch({
          status: 'loading',
          videoId,
          data: null,
          errorCode: null,
          errorMessage: null,
        }),
      ),
    completeAcquisition: (videoId, data) =>
      update(
        'acquisition:success',
        acquisitionPatch({
          status: 'success',
          videoId,
          data,
          errorCode: null,
          errorMessage: null,
        }),
      ),
    failAcquisition: (videoId, errorCode, errorMessage) =>
      update(
        'acquisition:error',
        acquisitionPatch({
          status: 'error',
          videoId,
          data: null,
          errorCode,
          errorMessage,
        }),
      ),
    clearAcquisition: () => update('acquisition:reset', acquisitionPatch(IDLE_ACQUISITION)),
    beginSentiment: (videoId) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale sentiment:begin ignored', { videoId });
        return;
      }
      update(
        'sentiment:loading',
        sentimentPatch({
          status: 'loading',
          videoId,
          data: null,
          errorCode: null,
          errorMessage: null,
        }),
      );
    },
    completeSentiment: (videoId, data) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale sentiment:success discarded', { videoId });
        return;
      }
      update(
        'sentiment:success',
        sentimentPatch({
          status: 'success',
          videoId,
          data,
          errorCode: null,
          errorMessage: null,
        }),
      );
    },
    failSentiment: (videoId, errorCode, errorMessage) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale sentiment:error discarded', { videoId });
        return;
      }
      update(
        'sentiment:error',
        sentimentPatch({
          status: 'error',
          videoId,
          data: null,
          errorCode,
          errorMessage,
        }),
      );
    },
    clearSentiment: () => update('sentiment:reset', sentimentPatch(IDLE_SENTIMENT)),
    beginTopics: (videoId) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale topics:begin ignored', { videoId });
        return;
      }
      update(
        'topics:loading',
        topicsPatch({
          status: 'loading',
          videoId,
          data: null,
          errorCode: null,
          errorMessage: null,
        }),
      );
    },
    completeTopics: (videoId, data) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale topics:success discarded', { videoId });
        return;
      }
      update(
        'topics:success',
        topicsPatch({
          status: 'success',
          videoId,
          data,
          errorCode: null,
          errorMessage: null,
        }),
      );
    },
    failTopics: (videoId, errorCode, errorMessage) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale topics:error discarded', { videoId });
        return;
      }
      update(
        'topics:error',
        topicsPatch({
          status: 'error',
          videoId,
          data: null,
          errorCode,
          errorMessage,
        }),
      );
    },
    clearTopics: () => update('topics:reset', topicsPatch(IDLE_TOPICS)),
    beginInsight: (videoId) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale insight:begin ignored', { videoId });
        return;
      }
      update(
        'insight:loading',
        insightPatch({
          status: 'loading',
          videoId,
          data: null,
          errorCode: null,
          errorMessage: null,
        }),
      );
    },
    completeInsight: (videoId, data) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale insight:success discarded', { videoId });
        return;
      }
      update(
        'insight:success',
        insightPatch({
          status: 'success',
          videoId,
          data,
          errorCode: null,
          errorMessage: null,
        }),
      );
    },
    failInsight: (videoId, errorCode, errorMessage) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale insight:error discarded', { videoId });
        return;
      }
      update(
        'insight:error',
        insightPatch({
          status: 'error',
          videoId,
          data: null,
          errorCode,
          errorMessage,
        }),
      );
    },
    clearInsight: () => update('insight:reset', insightPatch(IDLE_INSIGHT)),
    beginJob: (videoId, jobId) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale job:begin ignored', { videoId, jobId });
        return;
      }
      update(
        'job:start',
        jobPatch({
          status: 'running',
          videoId,
          jobId,
          job: null, // no counts until the first real poll lands (§7)
          errorCode: null,
          errorMessage: null,
        }),
      );
    },
    updateJob: (videoId, job) => {
      const current = state.job;
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale job:snapshot discarded', { videoId, jobId: job.jobId });
        return;
      }
      if (current.jobId !== null && current.jobId !== job.jobId) {
        // Superseded job snapshot (A -> B -> A): never mixes two runs.
        logger.debug('stale job:wrong job id discarded', {
          videoId,
          expected: current.jobId,
          got: job.jobId,
        });
        return;
      }
      update('job:snapshot', jobPatch({ ...current, status: 'running', job }));
    },
    failJob: (videoId, errorCode, errorMessage) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale job:error discarded', { videoId });
        return;
      }
      update(
        'job:error',
        jobPatch({
          ...state.job,
          status: 'error',
          videoId,
          errorCode,
          errorMessage,
        }),
      );
    },
    clearJob: () => update('job:reset', jobPatch(IDLE_JOB)),
    updateRealtime: (videoId, data) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale realtime:snapshot discarded', { videoId });
        return;
      }
      update(
        'realtime:snapshot',
        realtimePatch({
          status: 'success',
          videoId,
          data,
          errorCode: null,
          errorMessage: null,
        }),
      );
    },
    failRealtime: (videoId, errorCode, errorMessage) => {
      if (!isCurrentVideo(videoId)) {
        logger.debug('stale realtime:error discarded', { videoId });
        return;
      }
      // Keep the last known `data` (if any): a failed poll must never blank
      // a strip that is showing real, still-accurate aggregates (§15).
      update(
        'realtime:error',
        realtimePatch({
          ...state.realtime,
          status: 'error',
          videoId,
          errorCode,
          errorMessage,
        }),
      );
    },
    clearRealtime: () => update('realtime:reset', realtimePatch(IDLE_REALTIME)),
  };
}
