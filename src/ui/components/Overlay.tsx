import { useEffect, useRef } from 'react';
import { logger } from '../../lib/logger';
import type {
  AnalysisService,
  AnalysisServiceResult,
} from '../../services/analysis/analysis-service';
import type {
  AnalysisJobService,
} from '../../services/analysis/analysis-job-service';
import { asAcquisitionErrorCode, TERMINAL_JOB_STATUSES } from '../../services/analysis/analysis-job-service';
import type { SentimentService } from '../../services/analysis/sentiment-service';
import type { InsightService } from '../../services/analysis/insight-service';
import type { RealtimeService } from '../../services/analysis/realtime-service';
import type { TopicsService } from '../../services/analysis/topics-service';
import type { BackendGate } from '../../services/backend/backend-gate';
import { BACKEND_UNAVAILABLE_MESSAGE } from '../../services/backend/backend-gate';
import type { Store } from '../../state/store';
import type { OverlayState, OverlayStatus, PageKind, SectionId } from '../../shared/types';
import { OverlayBody } from './OverlayBody';
import { OverlayHeader } from './OverlayHeader';

interface OverlayProps {
  store: Store;
  state: OverlayState;
  analysisService: AnalysisService;
  sentimentService: SentimentService;
  /**
   * Sprint 4.3 background-job transport. Optional: without it the overlay
   * falls back to the legacy synchronous flow (dev shells + existing
   * tests). Production passes {@link HttpJobService}.
   */
  jobService?: AnalysisJobService;
  /**
   * Sprint 6 topic transport. Optional: without it the discussion section
   * simply stays absent (the honest offline stub reports unavailable -
   * no topics are ever fabricated). Production passes HttpTopicsService.
   */
  topicsService?: TopicsService;
  /**
   * Sprint 7 insight transport. Optional: without it the AI insight
   * section simply stays absent (the honest offline stub reports
   * unavailable - no summary text is ever fabricated client-side).
   * Production passes HttpInsightService.
   */
  insightService?: InsightService;
  /**
   * Sprint 8 realtime transport. Optional: without it the overlay simply
   * shows no live strip (the honest offline stub reports unavailable -
   * no movement is ever fabricated client-side). Production passes
   * {@link HttpRealtimeService}; its GET doubles as the backend
   * monitor's keep-alive touch.
   */
  realtimeService?: RealtimeService;
  /**
   * Sprint 4.4 backend availability gate (§15). Optional: without it the
   * flow assumes the backend is reachable (dev shells + existing tests).
   * Production passes {@link NativeBackendGate}.
   */
  backendGate?: BackendGate;
}

/**
 * Grace window after a freshly detected video context. YouTube scrolls the
 * page to top during SPA navigation; that scroll must not dismiss the overlay
 * that just auto-opened for the new video. Real user scrolls happen well
 * after this window (or keep scrolling past it, which then minimizes).
 */
const NAVIGATION_SCROLL_GRACE_MS = 750;

/**
 * Sprint 5.1 §8/§20: minimum interval between PROGRESSIVE sentiment
 * aggregate refreshes while a job runs (first insight). Aggregates only -
 * the overlay never receives per-comment updates - with at most one
 * in-flight request and only when the analyzed count actually grew.
 */
const PROGRESSIVE_REFRESH_MS = 1500;

/**
 * Sprint 8 §9 controlled status polling: the /realtime GET is local (no
 * YouTube quota) but still bounded - the loop runs at HALF the backend's
 * monitor interval, clamped into [5s, 10s], so a change is noticed within
 * one backend cycle while polling can never become uncontrolled. The
 * interval collapses to the cap while no successful status is known yet.
 */
const REALTIME_POLL_MIN_MS = 5000;
const REALTIME_POLL_MAX_MS = 10000;

/**
 * Sustained-scroll minimization: a quick flick must NOT collapse the overlay.
 * - The overlay minimizes only after SCROLL_CLOSE_DELAY_MS of continuous
 *   page scrolling (scroll events keep the session alive).
 * - A pause of SCROLL_IDLE_MS without scroll events cancels the session.
 * - At collapse time the scroll must also be recent (SCROLL_RECENT_MS), so
 *   decaying momentum from a flick never triggers minimization.
 * Scroll NEVER closes: OPEN → MINIMIZED only; CLOSED stays reserved for the
 * explicit ✕ / Escape actions.
 */
const SCROLL_CLOSE_DELAY_MS = 1000;
const SCROLL_IDLE_MS = 300;
const SCROLL_RECENT_MS = 200;

/**
 * Dark intelligence-console overlay shell.
 *
 * State machine (Sprint 4.4): a detected video auto-opens READY and the
 * automatic flow drives READY → (CONNECTING →) LOADING → COMPLETE ⇄
 * ANALYZING → COMPLETE | ERROR (Retry only from ERROR). There is no
 * Analyze button - video selection is the trigger (§4). Minimize is
 * orthogonal (component NOT unmounted), preserving state.
 *
 * The backend owns sentiment processing: this shell only GETs results and
 * renders them. Video identity guards (store-side + post-await checks) make
 * it impossible for Video A's analysis to render under Video B.
 *
 * Scroll behavior: sustained scrolling of the YouTube page (~1 s) MINIMIZES
 * the expanded overlay to the compact pill (OPEN → MINIMIZED - never
 * CLOSED). The overlay stays mounted, so the job poll, results, video
 * context, and analysis progress are all preserved; clicking the pill
 * restores it. A quick flick does nothing, and scrolls that originate
 * inside the overlay's own content are detected by source and ignored, so
 * reading long results never collapses the panel.
 */
export function Overlay({
  store,
  state,
  analysisService,
  sentimentService,
  jobService,
  topicsService,
  insightService,
  realtimeService,
  backendGate,
}: OverlayProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLButtonElement>(null);
  // Sprint 4.3 §27: the polling controller for the CURRENT video - aborted
  // on video change and on unmount, so Video A's status loop can never
  // outlive (or update) Video B's overlay.
  const jobAbortRef = useRef<AbortController | null>(null);
  // Sprint 5.1 §8: throttle bookkeeping for the in-run aggregate refresh.
  const progressRefreshRef = useRef({
    inFlight: false,
    lastAt: 0,
    lastAnalyzed: 0,
  });
  // Sprint 4.4 §7: the video id whose automatic flow already started - a
  // duplicate navigation/DOM event for the same id can never re-trigger.
  const autoHandledRef = useRef<string | null>(null);
  // Sprint 8 §11: last applied /realtime `version` per video - the cheap
  // flip marker that triggers a SILENT intelligence refetch (never a
  // phase change, never a visible reload).
  const realtimeVersionRef = useRef<{ videoId: string; version: string | null } | null>(
    null,
  );
  const videoId = state.videoContext?.videoId ?? null;
  const minimized = state.minimized;
  const busy =
    state.status === 'loading' ||
    state.status === 'analyzing' ||
    state.acquisition.status === 'loading' ||
    state.sentiment.status === 'loading';

  // OPEN → READY once the shell is mounted.
  useEffect(() => {
    if (store.getState().status === 'open') {
      store.setStatus('ready');
      logger.info('overlay opened');
    }
  }, [store]);

  // Abort background-job polling whenever the active video changes (or the
  // overlay unmounts) - polling stops on switch, exactly as §26 requires.
  useEffect(() => {
    return () => {
      jobAbortRef.current?.abort();
      jobAbortRef.current = null;
    };
  }, [videoId]);

  // Focus management: into panel on open/restore, into restore button on minimize.
  useEffect(() => {
    if (minimized) {
      restoreRef.current?.focus();
    } else {
      panelRef.current?.focus();
    }
  }, [minimized]);

  // YouTube page scroll → minimize the expanded overlay (compact pill remains).
  // - Passive capture listener on `document`: scroll events do not bubble,
  //   but the capture phase sees every scroll on the page at near-zero cost
  //   (passive: never preventDefault - YouTube's native scroll is untouched).
  // - Source detection: a scroll whose target is inside our UI (the panel, or
  //   the shadow host that hosts it) is an internal scroll → ignored.
  // - Navigation grace: scrolls fired right after a video context was
  //   detected (YouTube's scroll-to-top on SPA navigation) are ignored.
  // - Sustained-scroll session: collapsing requires ~1 s of continuous page
  //   scrolling; a quick flick (300 ms idle gap) cancels the session.
  // - State transition: OPEN → MINIMIZED via store.minimize(). Scroll never
  //   calls store.close(): the dialog shell (compact pill) stays mounted,
  //   the FAB-equivalent restore target remains visible, and the running
  //   analysis job/results are untouched (minimize is a visibility flag).
  // - Guards: while minimized (compact pill) or already closed this handler
  //   does nothing, so no repeated state updates can occur.
  useEffect(() => {
    if (minimized) return;
    const panel = panelRef.current;
    const rootNode = panel?.getRootNode();
    const host = rootNode instanceof ShadowRoot ? rootNode.host : null;

    const isInternalScroll = (target: EventTarget | null): boolean => {
      if (!(target instanceof Node)) return false;
      // In-page (non-shadow) renders: the scroller is a descendant of panel.
      if (panel && (target === panel || panel.contains(target))) return true;
      // In production the panel lives in the shadow tree, so at the document
      // boundary the event target is retargeted to the host (and non-composed
      // scroll events never leave the shadow tree in the first place).
      return host !== null && (target === host || host.contains(target));
    };

    // Scroll session state (only lives while the overlay is mounted).
    let commitTimer: ReturnType<typeof setTimeout> | null = null;
    let idleTimer: ReturnType<typeof setTimeout> | null = null;
    let lastScrollAt = 0;

    const resetSession = (): void => {
      if (commitTimer !== null) clearTimeout(commitTimer);
      if (idleTimer !== null) clearTimeout(idleTimer);
      commitTimer = null;
      idleTimer = null;
    };

    const onScroll = (event: Event): void => {
      if (isInternalScroll(event.target)) return;
      const current = store.getState();
      if (current.status === 'closed' || current.minimized) return; // idempotent
      // Navigation grace: detectedAt is refreshed synchronously by the
      // navigation detector, so YouTube's own scroll-to-top during an SPA
      // video change cannot close the just-opened overlay.
      const detectedAt = current.videoContext?.detectedAt ?? 0;
      if (Date.now() - detectedAt < NAVIGATION_SCROLL_GRACE_MS) return;

      const now = Date.now();
      lastScrollAt = now;

      // First scroll event of a session → schedule the minimize checkpoint.
      if (commitTimer === null) {
        commitTimer = setTimeout(() => {
          commitTimer = null;
          // Collapse only if the user is STILL actively scrolling at the
          // checkpoint: decaying momentum from a quick flick won't qualify.
          const active =
            Date.now() - lastScrollAt <= SCROLL_RECENT_MS &&
            store.getState().status !== 'closed' &&
            !store.getState().minimized;
          resetSession();
          if (active) {
            // OPEN → MINIMIZED, never OPEN → CLOSED (§FIX): the compact
            // pill remains visible, the overlay never unmounts, and the
            // job/results/context all stay intact.
            logger.info('overlay minimized by sustained page scroll');
            store.minimize();
          }
        }, SCROLL_CLOSE_DELAY_MS);
      }

      // Keep the session alive while scrolling continues; a pause of
      // SCROLL_IDLE_MS means the user stopped → cancel the pending close.
      if (idleTimer !== null) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        resetSession();
      }, SCROLL_IDLE_MS);
    };

    document.addEventListener('scroll', onScroll, { capture: true, passive: true });
    return () => {
      document.removeEventListener('scroll', onScroll, { capture: true });
      resetSession();
    };
  }, [minimized, store]);

  const onMinimize = (): void => store.minimize();
  const onRestore = (): void => store.restore();
  // Multi-section redesign: the ONLY section-switch entry point - a pure
  // store flip. No fetch, no refetch, no state-machine change (§16/§34).
  const onSelectSection = (section: SectionId): void => store.setSection(section);
  const onClose = (): void => {
    logger.info('overlay closed');
    store.close();
  };

  const onRetry = (): void => {
    store.setNotice(null);
    const current = store.getState();
    if (current.acquisition.status === 'error') {
      store.clearAcquisition();
    }
    if (current.sentiment.status === 'error') {
      store.clearSentiment();
    }
    // Sprint 6 §39: a failed topic fetch retries through the same flow.
    if (current.topics.status === 'error') {
      store.clearTopics();
    }
    if (current.insight.status === 'error') {
      store.clearInsight();
    }
    if (current.job.status === 'error') {
      store.clearJob();
    }
    if (store.getState().status === 'error') {
      store.setStatus('ready');
    }
    // §23: Retry is the ONLY manual action, and only from a failure state -
    // it restarts the full automatic pipeline (availability gate included).
    void startFlow();
  };

  // A result may land after the user minimized by scroll or closed the
  // overlay (Escape/✕). Record the outcome for this video, but never flip
  // the overlay status back from 'closed' - that would silently reopen it.
  // Minimized is NOT closed: interim results keep applying there, so
  // restoring always shows the latest real progress.
  const applyOverlayStatus = (next: OverlayStatus): void => {
    if (store.getState().status !== 'closed') store.setStatus(next);
  };

  /**
   * Sprint 4: fetch sentiment status/aggregates for one video. The backend
   * owns model execution and triggers pending processing inline; this only
   * GETs. Two stale guards: one before starting (video still active) and
   * one after the await (navigation may have happened mid-request). The
   * store applies a third, id-based guard on every action.
   */
  const loadSentiment = async (videoId: string, pageKind: PageKind): Promise<void> => {
    if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
    store.beginSentiment(videoId);
    applyOverlayStatus('analyzing');
    logger.debug('sentiment requested', { videoId });

    try {
      const result = await sentimentService.getSentiment({ videoId, pageKind });
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) {
        logger.debug('stale sentiment result discarded', { videoId });
        return;
      }
      if (result.kind === 'success') {
        store.completeSentiment(videoId, result.data);
        applyOverlayStatus('complete');
        logger.info('sentiment loaded', {
          videoId,
          analysisStatus: result.data.status,
          analyzed: result.data.stats.analyzed,
        });
      } else if (result.kind === 'unavailable') {
        // Honest offline/dev-stub outcome: stay in the READY TO ANALYZE state.
        store.clearSentiment();
        applyOverlayStatus('complete');
      } else {
        store.failSentiment(videoId, result.code, result.message);
        applyOverlayStatus('complete');
        logger.warn('sentiment failed', { videoId, code: result.code });
      }
    } catch (error) {
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
      logger.error('sentiment request failed unexpectedly', error);
      store.failSentiment(
        videoId,
        'acquisition_failed',
        "We couldn't retrieve the analysis. Try again.",
      );
      applyOverlayStatus('complete');
    }
  };

  /**
   * Sprint 6 §25/§40: fetch topic intelligence for one video AFTER the
   * sentiment result is final. Two stale guards like loadSentiment (before
   * starting + after the await); the store applies a third, video-id guard
   * on every action. Failures keep sentiment intact (§39) - the section
   * renders "Audience topics unavailable" and offers its own retry.
   */
  const loadTopics = async (videoId: string, pageKind: PageKind): Promise<void> => {
    if (!topicsService) return; // absent transport -> section stays hidden
    if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
    store.beginTopics(videoId);
    logger.debug('topics requested', { videoId });

    try {
      const result = await topicsService.getTopics({ videoId, pageKind });
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) {
        logger.debug('stale topics result discarded', { videoId });
        return;
      }
      if (result.kind === 'success') {
        store.completeTopics(videoId, result.data);
        logger.info('topics loaded', {
          videoId,
          status: result.data.status,
          topics: result.data.topics.length,
          analyzed: result.data.analyzedComments,
        });
      } else if (result.kind === 'unavailable') {
        // Honest offline/dev-stub outcome: no discussion section, no
        // fabricated themes (§38).
        store.clearTopics();
      } else {
        store.failTopics(videoId, result.code, result.message);
        logger.warn('topics failed', { videoId, code: result.code });
      }
    } catch (error) {
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
      logger.error('topics request failed unexpectedly', error);
      store.failTopics(
        videoId,
        'acquisition_failed',
        "We couldn't retrieve audience topics.",
      );
    }
  };

  /**
   * Sprint 6 trigger: exactly once per video, only when sentiment is
   * final (PROCESSED) AND no background job is still running - interim
   * aggregates during a run would otherwise pin a partial topic snapshot
   * (the backend memo recomputes on any analysis change; the slice is
   * cleared on refresh/video change and fetched again).
   */
  useEffect(() => {
    if (videoId === null || !topicsService) return;
    const current = store.getState();
    if (current.topics.status !== 'idle') return; // one fetch per video
    if (current.sentiment.status !== 'success') return;
    if (current.sentiment.videoId !== videoId) return;
    if (current.sentiment.data?.status !== 'PROCESSED') return;
    const job = current.job;
    if (job.status === 'error') return; // failure flow owns the screen (§39 retry)
    if (
      job.status === 'running' &&
      (job.job === null || !TERMINAL_JOB_STATUSES.has(job.job.status))
    ) {
      return; // wait for the job's terminal snapshot
    }
    void loadTopics(videoId, current.videoContext?.pageKind ?? 'watch');
    // loadTopics reads refs/stores stable for the overlay's lifetime; the
    // trigger identity is (video id, sentiment, job, topics slice). The
    // topics slice IS a dependency on purpose: clearTopics (Retry §39 /
    // sentiment re-analysis) returns it to idle and must re-trigger the
    // fetch, while loading/success/error guards prevent duplicates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId, topicsService, state.sentiment, state.job, state.topics, store]);

  /**
   * Sprint 5.1 §8/§9: throttled PROGRESS refresh of the sentiment aggregate
   * WHILE the job is still running (time-to-first-insight).
   *
   * Contract:
   * - aggregates only (one GET, never per-comment updates - §20);
   * - at most one request in flight, at most one per
   *   PROGRESSIVE_REFRESH_MS, and only when `analyzed` actually grew;
   * - it never changes the overlay phase - OverlayBody renders the data
   *   behind an explicit ANALYZING state until the job is terminal, so an
   *   interim aggregate can never be mistaken for the final result (§8);
   * - transport errors are silently retried on the next tick: the job poll
   *   remains the source of truth for progress and failures.
   */
  const refreshSentimentProgress = async (
    videoId: string,
    pageKind: PageKind,
    analyzed: number,
  ): Promise<void> => {
    const tracker = progressRefreshRef.current;
    if (tracker.inFlight) return;
    const now = Date.now();
    if (now - tracker.lastAt < PROGRESSIVE_REFRESH_MS) return;
    if (analyzed <= tracker.lastAnalyzed) return;
    tracker.inFlight = true;
    tracker.lastAt = now;
    tracker.lastAnalyzed = analyzed;
    try {
      const result = await sentimentService.getSentiment({ videoId, pageKind });
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) {
        logger.debug('stale progressive sentiment discarded', { videoId });
        return;
      }
      if (result.kind === 'success') {
        store.completeSentiment(videoId, result.data);
        logger.debug('progressive sentiment snapshot applied', {
          videoId,
          analyzed: result.data.stats.analyzed,
        });
      }
    } catch (error) {
      logger.debug('progressive sentiment refresh skipped', { videoId });
    } finally {
      tracker.inFlight = false;
    }
  };

  /**
   * Sprint 7 §26/§40: fetch the audience insight for one video AFTER the
   * sentiment result is final. Two stale guards like loadSentiment (before
   * starting + after the await); the store applies a third, video-id guard
   * on every action. Failures keep sentiment/topics intact (§30) - the
   * section renders "Audience insight unavailable" with its own Retry.
   */
  const loadInsight = async (videoId: string, pageKind: PageKind): Promise<void> => {
    if (!insightService) return; // absent transport -> section stays hidden
    if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
    store.beginInsight(videoId);
    logger.debug('insight requested', { videoId });

    try {
      const result = await insightService.getInsight({ videoId, pageKind });
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) {
        logger.debug('stale insight result discarded', { videoId });
        return;
      }
      if (result.kind === 'success') {
        store.completeInsight(videoId, result.data);
        logger.info('insight loaded', {
          videoId,
          status: result.data.status,
          source: result.data.source,
          cards: result.data.cards.length,
        });
      } else if (result.kind === 'unavailable') {
        // Honest offline/dev-stub outcome: no insight section, no
        // fabricated summary (§16/§31).
        store.clearInsight();
      } else {
        store.failInsight(videoId, result.code, result.message);
        logger.warn('insight failed', { videoId, code: result.code });
      }
    } catch (error) {
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
      logger.error('insight request failed unexpectedly', error);
      store.failInsight(
        videoId,
        'acquisition_failed',
        "We couldn't retrieve the audience insight.",
      );
    }
  };

  /**
   * Sprint 7 trigger: exactly once per video, only when sentiment is
   * final (PROCESSED) AND no background job is still running - the same
   * identity as the topics trigger so both intelligence sections settle
   * together. The insight slice IS a dependency on purpose: clearInsight
   * (Retry / re-analysis) returns it to idle and must re-trigger.
   */
  useEffect(() => {
    if (videoId === null || !insightService) return;
    const current = store.getState();
    if (current.insight.status !== 'idle') return; // one fetch per video
    if (current.sentiment.status !== 'success') return;
    if (current.sentiment.videoId !== videoId) return;
    if (current.sentiment.data?.status !== 'PROCESSED') return;
    const job = current.job;
    if (job.status === 'error') return; // failure flow owns the screen (§30 retry)
    if (
      job.status === 'running' &&
      (job.job === null || !TERMINAL_JOB_STATUSES.has(job.job.status))
    ) {
      return; // wait for the job's terminal snapshot
    }
    void loadInsight(videoId, current.videoContext?.pageKind ?? 'watch');
    // loadInsight reads refs/stores stable for the overlay's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId, insightService, state.sentiment, state.job, state.insight, store]);

  /**
   * Sprint 8 §11: SILENT intelligence refresh after a /realtime version
   * flip. Re-Gets sentiment without touching the overlay phase (never a
   * spinner, never a status flip - the panel keeps rendering the previous
   * real numbers until the new ones land), then clears the topic/insight
   * slices so their existing once-per-change triggers refetch them. All
   * three are backend-owned reads; nothing is derived client-side.
   */
  const refreshIntelligence = async (
    videoId: string,
    pageKind: PageKind,
  ): Promise<void> => {
    if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
    try {
      const result = await sentimentService.getSentiment({ videoId, pageKind });
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) {
        logger.debug('stale realtime sentiment refresh discarded', { videoId });
        return;
      }
      if (result.kind !== 'success') {
        // Keep the last known aggregates; the next version flip retries.
        logger.debug('realtime sentiment refresh skipped', {
          videoId,
          kind: result.kind,
        });
        return;
      }
      store.completeSentiment(videoId, result.data); // silent: no phase change
      if (result.data.status === 'PROCESSED') {
        // The verdict set moved -> topic/insight snapshots are stale.
        // Clearing re-triggers their fetch effects, so both sections
        // update automatically - no Analyze, no Refresh (§19).
        store.clearTopics();
        store.clearInsight();
      }
      logger.debug('realtime intelligence refresh applied', {
        videoId,
        analyzed: result.data.stats.analyzed,
      });
    } catch (error) {
      logger.debug('realtime intelligence refresh failed', { videoId });
    }
  };

  /**
   * Sprint 8 §9/§15: the controlled realtime polling loop.
   *
   * Identity: one loop per (video, sentiment-final) window - the same gates
   * as the topics/insight triggers, so polling starts once the dataset
   * exists and stops on video change, overlay close (unmount) and job
   * error (Retry owns the screen). The GET is ALSO the backend monitor's
   * keep-alive touch: while the overlay polls, the backend checks YouTube
   * for new comments on its own configured interval (never from a request
   * thread - §15: handlers stay non-blocking).
   *
   * On a `version` flip (stored or analyzed actually changed - §4: real
   * data only) it runs the silent refresh above. Failures keep the last
   * known strip and retry on the bounded cap interval (§15).
   */
  useEffect(() => {
    if (videoId === null || !realtimeService) return;
    const current = store.getState();
    if (current.sentiment.status !== 'success') return; // dataset must be final
    if (current.sentiment.videoId !== videoId) return;
    if (current.sentiment.data?.status !== 'PROCESSED') return;
    const job = current.job;
    if (job.status === 'error') return; // failure flow owns the screen
    if (
      job.status === 'running' &&
      (job.job === null || !TERMINAL_JOB_STATUSES.has(job.job.status))
    ) {
      return; // wait for the job's terminal snapshot
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const pageKind = current.videoContext?.pageKind ?? 'watch';
    const service = realtimeService;

    // Version bookkeeping is per video: a reopened overlay (fresh mount)
    // re-applies the current version without a spurious refresh storm.
    if (realtimeVersionRef.current?.videoId !== videoId) {
      realtimeVersionRef.current = { videoId, version: null };
    }

    const schedule = (ms: number): void => {
      if (cancelled) return;
      timer = setTimeout(() => {
        void poll();
      }, ms);
    };

    const poll = async (): Promise<void> => {
      if (cancelled) return;
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
      try {
        const result = await service.getRealtime({ videoId, pageKind });
        if (cancelled) return;
        if ((store.getState().videoContext?.videoId ?? null) !== videoId) {
          logger.debug('stale realtime poll discarded', { videoId });
          return;
        }
        if (result.kind === 'success') {
          const previous = realtimeVersionRef.current?.version ?? null;
          store.updateRealtime(videoId, result.data);
          if (previous !== null && previous !== result.data.version) {
            // Real change behind the marker -> silent refetch (§11).
            void refreshIntelligence(videoId, pageKind);
          }
          realtimeVersionRef.current = { videoId, version: result.data.version };
          schedule(
            Math.min(
              REALTIME_POLL_MAX_MS,
              Math.max(
                REALTIME_POLL_MIN_MS,
                Math.round(result.data.pollIntervalSeconds * 500),
              ),
            ),
          );
        } else if (result.kind === 'error') {
          // Keep the last known strip; bounded retry (§15 recovery).
          store.failRealtime(videoId, result.code, result.message);
          schedule(REALTIME_POLL_MAX_MS);
        } else {
          // Honest unavailable stub: no strip, quietly keep the cadence.
          schedule(REALTIME_POLL_MAX_MS);
        }
      } catch (error) {
        if (!cancelled) schedule(REALTIME_POLL_MAX_MS);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
    // state.realtime is deliberately NOT a dependency: each successful
    // poll updates it, and restarting the loop on its own output would
    // starve the timer. The loop restarts only when its GATES change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId, realtimeService, state.sentiment, state.job, store]);

  /**
   * Sprint 6 §39: topic-only retry - drops the failed slice and lets the
   * trigger effect re-fetch (sentiment is never re-run by this action).
   */
  const onTopicsRetry = (): void => {
    store.clearTopics();
  };

  /** Sprint 7 §29: insight-only retry - only ever called from FAILED. */
  const onInsightRetry = (): void => {
    store.clearInsight();
  };

  const onSentimentRetry = (): void => {
    const context = store.getState().videoContext;
    if (!context?.videoId || busy) return;
    store.setNotice(null);
    // Sprint 6: a re-analysis invalidates the topic snapshot too - the
    // trigger effect re-fetches once the new result is PROCESSED.
    store.clearTopics();
    // Sprint 7: and so does the insight (its evidence IS that analysis).
    store.clearInsight();
    if (jobService) {
      // Sprint 4.3: re-run through the background job (fast fresh-data
      // path) instead of holding one long sentiment request open.
      void onAnalyze();
      return;
    }
    void loadSentiment(context.videoId, context.pageKind);
  };

  // Header refresh: re-request sentiment when data is already acquired,
  // otherwise fall through to the full acquisition flow. Blocked while the
  // availability gate is still running (the flow owns the transition).
  const onRefresh = (): void => {
    const current = store.getState();
    if (current.status === 'connecting') return;
    if (
      current.acquisition.status === 'success' &&
      current.acquisition.videoId === current.videoContext?.videoId
    ) {
      onSentimentRetry();
      return;
    }
    void onAnalyze();
  };

  const onAnalyze = async (): Promise<void> => {
    const context = store.getState().videoContext;
    if (!context?.videoId || busy) return;
    const videoId = context.videoId;
    store.setNotice(null);
    store.setStatus('loading');
    store.beginAcquisition(videoId);
    logger.debug('acquisition requested', { videoId });

    // Sprint 4.3: START the pipeline as a background job first (§40 - the
    // browser request never waits for pages/batches), poll it with real
    // counts, then read the finished results with two fast GETs.
    if (jobService) {
      const started = await jobService.start({ videoId, pageKind: context.pageKind });
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) {
        logger.debug('stale job start discarded', { videoId });
        return;
      }
      if (started.kind === 'error') {
        store.failAcquisition(videoId, started.code, started.message);
        applyOverlayStatus('error');
        logger.warn('analysis job failed to start', { videoId, code: started.code });
        return;
      }
      store.beginJob(videoId, started.job.jobId);
      // New run, fresh throttle bookkeeping (a previous video's snapshot
      // must never gate this video's first-insight refresh).
      progressRefreshRef.current = {
        inFlight: false,
        lastAt: 0,
        lastAnalyzed: 0,
      };
      logger.info('analysis job started', {
        videoId,
        jobId: started.job.jobId,
        status: started.job.status,
      });

      const controller = new AbortController();
      jobAbortRef.current = controller;
      const waited = await jobService.waitForTerminal({
        videoId,
        pageKind: context.pageKind,
        signal: controller.signal,
        onJob: (job) => {
          store.updateJob(videoId, job); // store-side video + job-id guards
          if ((store.getState().videoContext?.videoId ?? null) !== videoId) return;
          // Phase drives the visible state: ACQUISITION -> LOADING,
          // SENTIMENT + TOPIC + INSIGHT -> ANALYZING (§25 progress states;
          // the Sprint 6/7 phases name their step in the live view).
          if (job.phase === 'ACQUISITION') applyOverlayStatus('loading');
          else if (
            job.phase === 'SENTIMENT' ||
            job.phase === 'TOPIC' ||
            job.phase === 'INSIGHT'
          ) applyOverlayStatus('analyzing');
          // Sprint 5.1 §8: once a batch carries real verdicts, pull the
          // aggregate (throttled) so first insight appears DURING the run
          // instead of only after completion.
          if (job.analyzed > 0 && !TERMINAL_JOB_STATUSES.has(job.status)) {
            void refreshSentimentProgress(videoId, context.pageKind, job.analyzed);
          }
        },
      });
      // Sprint 4.4: a superseded run's late completion must never clear
      // the CURRENT run's polling handle (rapid A → B switches).
      if (jobAbortRef.current === controller) {
        jobAbortRef.current = null;
      }

      if (waited.kind === 'aborted') {
        logger.debug('job polling stopped', { videoId });
        return;
      }
      if ((store.getState().videoContext?.videoId ?? null) !== videoId) {
        logger.debug('stale job result discarded', { videoId });
        return;
      }
      if (waited.kind !== 'completed') {
        const code =
          waited.kind === 'failed'
            ? asAcquisitionErrorCode(waited.job.errorCode)
            : waited.code;
        const message =
          waited.kind === 'failed'
            ? (waited.job.errorMessage ??
              'Analysis was interrupted before completion. Please retry.')
            : waited.message;
        // §25 FAILED: keep the last real snapshot for the collected count.
        store.clearAcquisition();
        store.failJob(videoId, code, message);
        applyOverlayStatus('error');
        logger.warn('analysis job terminated', { videoId, code });
        return;
      }
      logger.info('analysis job completed', {
        videoId,
        collected: waited.job.collected,
        analyzed: waited.job.analyzed,
        skipped: waited.job.skipped,
      });
    }

    try {
      const result: AnalysisServiceResult = await analysisService.analyze({
        videoId,
        pageKind: context.pageKind,
      });

      // Stale guard: the user may have navigated while we waited.
      const currentId = store.getState().videoContext?.videoId ?? null;
      if (currentId !== videoId) {
        logger.debug('stale acquisition result discarded', { videoId, currentId });
        return;
      }

      if (result.kind === 'success') {
        // Data is recorded even if the overlay was dismissed meanwhile: it
        // belongs to the still-active video, so reopening shows it without a
        // duplicate backend/YouTube API request.
        store.completeAcquisition(videoId, result.data);
        applyOverlayStatus('complete');
        logger.info('acquisition succeeded', {
          videoId,
          comments: result.data.comments.count,
          status: result.data.comments.status,
        });
        // Sprint 4: acquisition complete -> request sentiment (backend runs
        // the model inline on first request; subsequent GETs are reads).
        await loadSentiment(videoId, context.pageKind);
      } else if (result.kind === 'unavailable') {
        // Honest offline/dev-stub outcome: nothing was acquired.
        store.clearAcquisition();
        applyOverlayStatus('ready');
        if (store.getState().status !== 'closed') {
          store.setNotice(
            'Analysis services will be connected in the next development phase. No results were generated.',
          );
        }
      } else {
        store.failAcquisition(videoId, result.code, result.message);
        applyOverlayStatus('error');
        logger.warn('acquisition failed', { videoId, code: result.code });
      }
    } catch (error) {
      const currentId = store.getState().videoContext?.videoId ?? null;
      if (currentId !== videoId) return;
      logger.error('acquisition request failed unexpectedly', error);
      store.failAcquisition(
        videoId,
        'acquisition_failed',
        "We couldn't retrieve audience responses for this video. Please try again later.",
      );
      applyOverlayStatus('error');
    }
  };

  /**
   * Sprint 4.4 automatic pipeline (§8): detected video → backend
   * availability gate (§15, bounded) → background job (Sprint 4.3) →
   * real progress → results. Never called by any button in the normal
   * flow - the ONLY callers are the video-detection effect below and the
   * error-state Retry (§23).
   */
  const startFlow = async (): Promise<void> => {
    const flowId = store.getState().videoContext?.videoId ?? null;
    if (flowId === null) return;

    if (backendGate) {
      applyOverlayStatus('connecting');
      const gate = await backendGate.ensure();
      // The user may have navigated (or closed) while the gate was running.
      if ((store.getState().videoContext?.videoId ?? null) !== flowId) {
        logger.debug('stale backend gate result discarded', { videoId: flowId });
        return;
      }
      if (gate.kind !== 'ready') {
        // §30: friendly FAILED state - never connection errors or traces.
        store.clearAcquisition();
        store.failJob(flowId, 'network_error', BACKEND_UNAVAILABLE_MESSAGE);
        applyOverlayStatus('error');
        logger.warn('analysis blocked: backend unavailable', { reason: gate.reason });
        return;
      }
      logger.debug('backend gate ready', { outcome: gate.outcome });
    }
    await onAnalyze();
  };

  // §4/§17: video detection IS the trigger. Fires once per video id
  // (autoHandledRef + the store's same-id dedupe), on overlay mount
  // (initial detection, reopen) and on every genuine video change. Failure
  // states are left alone so the user's Retry (§23) is the next step.
  useEffect(() => {
    if (videoId === null) return;
    if (autoHandledRef.current === videoId) return;
    autoHandledRef.current = videoId;

    const current = store.getState();
    if (current.status === 'error') return; // FAILED: waits for Retry (§23)
    if (current.job.status === 'error' && current.job.videoId === videoId) return;
    if (current.acquisition.status === 'error') return;
    if (current.sentiment.status === 'error') return;
    // Reopened overlay with results already on screen: never re-analyze
    // the same video (§7/§24 - no duplicate jobs, no cache re-hammering).
    if (current.acquisition.videoId === videoId && current.acquisition.status === 'success') {
      return;
    }
    if (current.sentiment.videoId === videoId && current.sentiment.status === 'success') {
      return;
    }
    void startFlow();
    // startFlow only reads refs/stores that are stable for the overlay's
    // lifetime; the trigger identity is the video id itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId, store]);

  return (
    // Geometry is px-locked to the design (top 72, height min(640, 100vh−96)).
    // Never use rem here: the host page controls the root font-size
    // (YouTube sets 10px → 6rem = 60px → the panel bottom overflowed the
    // viewport by 12px and clipped the footer).
    <div
      ref={panelRef}
      tabIndex={-1}
      role="dialog"
      aria-label="AI Analyzer overlay"
      className={`sai-glass sai-panel sai-focus fixed top-[72px] right-4 flex flex-col outline-none ${
        minimized
          ? 'w-auto rounded-full'
          : 'h-[min(640px,calc(100vh_-_96px))] w-[380px] max-w-[calc(100%_-_2rem)] rounded-2xl'
      }`}
    >
      <OverlayHeader
        status={state.status}
        minimized={minimized}
        videoId={state.videoContext?.videoId ?? null}
        restoreRef={restoreRef}
        onMinimize={onMinimize}
        onRestore={onRestore}
        onRefresh={onRefresh}
        onClose={onClose}
        refreshDisabled={busy || state.status === 'connecting'}
      />

      {!minimized && state.notice && (
        <p role="status" aria-live="polite" className="sai-notice">
          {state.notice}
        </p>
      )}

      {!minimized && (
        <div className="sai-body min-h-0 flex-1 overflow-y-auto px-4 py-4">
      <OverlayBody
        state={state}
        onRetry={onRetry}
        onSentimentRetry={onSentimentRetry}
        onTopicsRetry={onTopicsRetry}
        onInsightRetry={onInsightRetry}
        onSelectSection={onSelectSection}
      />
        </div>
      )}

      {!minimized && (
        <footer className="sai-footer flex items-center justify-between border-t px-4 py-2 text-[10px] tracking-[0.2em] uppercase">
          <span>Sentiment Intelligence</span>
          <span>Sprint 8</span>
        </footer>
      )}
    </div>
  );
}
