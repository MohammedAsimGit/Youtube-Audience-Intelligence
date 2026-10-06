import { useEffect, useRef, useState, type ReactNode } from 'react';
import { isYouTubeUrl } from '../../services/youtube/detection';
import { TERMINAL_JOB_STATUSES } from '../../services/analysis/analysis-job-service';
import type {
  AcquisitionErrorCode,
  AnalysisJobPhase,
  JobState,
  OverlayState,
  SectionId,
  SentimentAnalysis,
  VideoDataResponse,
} from '../../shared/types';
import { AlertIcon, SparkleIcon, YoutubeIcon } from './icons';
import { STATUS_LABELS } from './StatusChip';
import { formatPercent, nf } from '../format';
import {
  AudienceHero,
  ConfidenceCard,
  CoverageCard,
  EmotionCard,
  IntensityCard,
  MoodCard,
  QuickGlanceCards,
  SentimentDistributionCard,
  TechnicalDetails,
} from './insights';
import { InsightSection } from './insight';
import { TopicSection } from './topics';
import { AudienceMovementCard, RealtimeStrip } from './realtime';
import { SectionNav } from './SectionNav';
import { SaiCard } from './ui';

interface OverlayBodyProps {
  state: OverlayState;
  onRetry: () => void;
  onSentimentRetry: () => void;
  /** Sprint 6 §39: topic-only retry (re-fetch, never re-runs sentiment). */
  onTopicsRetry: () => void;
  /** Sprint 7 §29: insight-only retry (only from the FAILED state). */
  onInsightRetry: () => void;
  /**
   * Multi-section redesign: UI-only section switch, wired to the store's
   * `setSection` by the overlay shell. Optional so direct body renders
   * (tests/dev shells) stay valid; the section state itself always comes
   * from `state.activeSection` - never local component state.
   */
  onSelectSection?: (section: SectionId) => void;
}

interface MessageViewProps {
  title?: string;
  message: string;
  hint?: string;
  /** Optional small monospace fact line (e.g. exact dataset counts). */
  detail?: ReactNode;
  action?: { label: string; onClick: () => void };
}

/** Shared centered message layout for unsupported/error/empty states. */
function MessageView({ title, message, hint, detail, action }: MessageViewProps) {
  return (
    <div className="flex flex-col items-center gap-2.5 py-9 text-center">
      <AlertIcon className="h-6 w-6 text-amber-300/90" />
      {title && <p className="text-[13px] font-semibold tracking-wide text-slate-100">{title}</p>}
      <p className="max-w-[36ch] text-[12.5px] leading-relaxed text-slate-300">{message}</p>
      {hint && <p className="max-w-[36ch] text-[11.5px] leading-relaxed text-slate-400">{hint}</p>}
      {detail && (
        <p className="sai-mono max-w-[36ch] text-[11px] leading-relaxed text-slate-400">
          {detail}
        </p>
      )}
      {action && (
        <button type="button" onClick={action.onClick} className="sai-btn mt-1">
          {action.label}
        </button>
      )}
    </div>
  );
}

/**
 * Sprint 4.4 §15/§19 CONNECTING: shown while the bounded backend
 * availability gate runs (health probe → native host → health re-probe).
 * Resolves automatically to analysis or to the friendly FAILED state.
 */
function ConnectingView() {
  return (
    <div className="flex flex-col items-center gap-3 py-9 text-center">
      <div className="sai-spinner" aria-hidden="true" />
      <p className="sai-hud">CONNECTING</p>
      <p className="text-sm text-slate-200">Detecting audience response…</p>
      <p className="max-w-[34ch] text-[11.5px] leading-relaxed text-slate-400">
        Starting the local AI service. Analysis begins automatically as soon as it is ready.
      </p>
    </div>
  );
}

/**
 * READY → LOADING (honest progress). With a Sprint 4.3 background job the
 * view mirrors §25: QUEUED shows "Preparing analysis...", ACQUIRING shows
 * the REAL collected count from the latest status poll - never a
 * fabricated percentage or estimate.
 */
function AcquiringView({ jobState }: { jobState: JobState }) {
  const tracking = jobState.status === 'running';
  const snapshot = tracking ? jobState.job : null;
  const queued =
    tracking && (snapshot === null || snapshot.status === 'QUEUED');
  return (
    <div className="flex flex-col items-center gap-3 py-9 text-center">
      <div className="sai-spinner" aria-hidden="true" />
      <p className="sai-hud">Acquiring Data</p>
      <p className="text-sm text-slate-200">
        {queued ? 'Preparing analysis…' : 'Collecting audience responses…'}
      </p>
      {snapshot !== null &&
        snapshot.status !== 'QUEUED' &&
        snapshot.collected > 0 && (
          <p
            className="sai-mono text-[12px] text-violet-200"
            role="status"
            aria-live="polite"
          >
            {nf(snapshot.collected)} comments collected
          </p>
        )}
      <p className="max-w-[34ch] text-[11.5px] leading-relaxed text-slate-400">
        Retrieving real video metadata and comments through the backend. No sentiment analysis
        has been performed yet.
      </p>
    </div>
  );
}

/**
 * Sprint 4.3 §25 FAILED state: a background job that died shows the honest
 * interrupted copy with the REAL counts of the last polled snapshot and a
 * Retry Analysis action (never a generic "something went wrong").
 */
function JobFailedView({
  state,
  onRetry,
}: Pick<OverlayBodyProps, 'state' | 'onRetry'>) {
  const jobState = state.job;
  const snapshot = jobState.job;
  const code = jobState.errorCode;
  const offline = code === 'network_error' || code === 'server_not_configured';
  const title = offline
    ? 'BACKEND UNAVAILABLE'
    : code === 'quota_exceeded'
      ? 'QUOTA EXCEEDED'
      : 'ANALYSIS INTERRUPTED';
  const message =
    jobState.errorMessage ??
    'Analysis was interrupted before completion. Please retry.';
  // Real counts from the last status poll (§7): collected, plus analyzed
  // when the job got as far as sentiment.
  const detail =
    snapshot === null
      ? undefined
      : snapshot.analyzed > 0
        ? `${nf(snapshot.collected)} comments collected · ${nf(snapshot.analyzed)} analyzed`
        : `${nf(snapshot.collected)} comments collected`;
  return (
    <MessageView
      title={title}
      message={message}
      hint={
        offline
          ? 'The local AI service could not be reached. Press Retry - if it keeps failing, restart the service (see README troubleshooting).'
          : undefined
      }
      detail={detail}
      action={{ label: 'Retry', onClick: onRetry }}
    />
  );
}

const ERROR_TITLES: Partial<Record<AcquisitionErrorCode, string>> = {
  video_not_found: 'VIDEO UNAVAILABLE',
  invalid_video_id: 'VIDEO UNAVAILABLE',
  quota_exceeded: 'QUOTA EXCEEDED',
  network_error: 'BACKEND UNAVAILABLE',
  server_not_configured: 'BACKEND UNAVAILABLE',
};

function AcquisitionErrorView({ state, onRetry }: Pick<OverlayBodyProps, 'state' | 'onRetry'>) {
  const acquisition = state.acquisition;
  const code = acquisition.errorCode;
  const title = (code && ERROR_TITLES[code]) || 'DATA ACQUISITION FAILED';
  const message =
    acquisition.errorMessage ??
    state.notice ??
    "We couldn't retrieve audience responses for this video. Please try again later.";
  const hint =
    code === 'server_not_configured' || code === 'network_error'
      ? 'The local AI service could not be reached. Press Retry - if it keeps failing, restart the service (see README troubleshooting).'
      : undefined;
  return (
    <MessageView title={title} message={message} hint={hint} action={{ label: 'Retry', onClick: onRetry }} />
  );
}

function CommentsUnavailableBlock({ status }: { status: 'none' | 'disabled' }) {
  return (
    <div className="sai-inset mt-2 space-y-1.5 rounded-xl px-3 py-2.5">
      <p className="text-[12px] font-semibold tracking-wide text-amber-300/90">
        COMMENTS UNAVAILABLE
      </p>
      <p className="text-[11.5px] leading-relaxed text-slate-400">
        {status === 'disabled'
          ? 'Comments are disabled for this video.'
          : 'This video currently has no accessible audience comments.'}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sprint 4: sentiment intelligence sections (backend-owned results only)
// ---------------------------------------------------------------------------

/** Honest pipeline copy for an in-flight run (§25 - steps, not a bare spinner). */
const PIPELINE_STEPS = [
  'Reading discussion',
  'Processing comments',
  'Classifying sentiment',
  'Aggregating results',
] as const;

/**
 * Live progress is shown ONLY with backend-reported counts (§30 - never a
 * fabricated percentage); without data this stays a step list.
 */
function AnalyzingView({
  progress,
}: {
  progress?: { stored: number; analyzed: number; remaining: number };
}) {
  return (
    <div className="sai-pipeline" role="status" aria-live="polite">
      <p className="sai-analysis-state">
        <SparkleIcon className="sai-pulse-icon h-3.5 w-3.5" />
        ANALYZING AUDIENCE
      </p>
      {/* §15: a human sentence above the step list - the AI state should
          feel alive without faking any progress numbers. */}
      <p className="sai-friendly">Reading audience reactions…</p>
      <ol className="sai-steps">
        {PIPELINE_STEPS.map((step) => (
          <li key={step} className="sai-step">
            {step}
          </li>
        ))}
      </ol>
      {progress && (
        <p className="sai-mono text-[11px] text-slate-400">
          {nf(progress.stored)} collected · {nf(progress.analyzed)} analyzed ·{' '}
          {nf(progress.remaining)} remaining
        </p>
      )}
    </div>
  );
}

/**
 * §3: the happy path never offers an action. Results land automatically
 * (§22); this quiet block only explains where they will appear when the
 * backend reports no analyzed rows yet.
 */
function AwaitingAnalysisView() {
  return (
    <div className="sai-inset space-y-2 rounded-xl px-3.5 py-3">
      <p className="sai-analysis-state">AWAITING ANALYSIS</p>
      <p className="text-[11.5px] leading-relaxed text-slate-300">
        Sentiment results appear here automatically as soon as the analysis completes.
      </p>
    </div>
  );
}

function SentimentErrorView({
  errorCode,
  message,
  onRetry,
}: {
  errorCode: AcquisitionErrorCode | null;
  message: string;
  onRetry: () => void;
}) {
  const offline =
    errorCode === 'network_error' || errorCode === 'server_not_configured';
  return (
    <MessageView
      title={offline ? 'AI SERVICE OFFLINE' : 'ANALYSIS UNAVAILABLE'}
      message={message}
      hint={offline ? 'Check your connection and try again.' : undefined}
      action={{ label: 'Retry', onClick: onRetry }}
    />
  );
}

function NoAudienceDataView() {
  return (
    <MessageView
      title="NO AUDIENCE DATA"
      message="There are no comments available for analysis on this video."
    />
  );
}

function InsufficientLanguageView({ data }: { data: SentimentAnalysis }) {
  const { stats, dataset } = data;
  return (
    <MessageView
      title="INSUFFICIENT LANGUAGE SUPPORT"
      message="The available comments could not be analyzed reliably."
      hint={`${nf(dataset.skipped)} stored comments are in languages the current model does not support.`}
      // §29: exact counts, denominator visible - never a fake 0% distribution.
      detail={`Collected ${nf(dataset.collected)} · Analyzed ${nf(stats.analyzed)} · Skipped ${nf(stats.skipped)}`}
    />
  );
}

const SENTIMENT_ROW_KINDS = ['positive', 'neutral', 'negative'] as const;

/**
 * Sprint 5.1 §8/§9: the FIRST real aggregate while the job is still running.
 *
 * Every number is backend-derived from ONE consistent sentiment snapshot
 * (percentages and counts come from the same response - no mixing), and the
 * ANALYZING badge plus the "of stored comments" denominator make it
 * unmistakable that this is an in-flight result that keeps updating. Never
 * presented as final, never fabricated or extrapolated: the final view only
 * takes over when the job reaches a terminal state.
 */
function ProgressiveInsightView({
  data,
  phase,
}: {
  data: SentimentAnalysis;
  /** Live job phase - TOPIC names the real Sprint 6 discovery step (§36). */
  phase?: AnalysisJobPhase;
}) {
  const { stats, dataset } = data;
  const rows = [
    { label: 'Positive', count: stats.positive, pct: stats.positivePercent },
    { label: 'Neutral', count: stats.neutral, pct: stats.neutralPercent },
    { label: 'Negative', count: stats.negative, pct: stats.negativePercent },
  ];
  return (
    <div role="status" aria-live="polite">
      <SaiCard label="Audience sentiment in progress" className="sai-card-live">
        <div className="flex items-baseline justify-between gap-2">
          <h2 className="sai-hud">Audience Sentiment</h2>
          <span className="sai-state-badge text-violet-200">ANALYZING…</span>
        </div>

        <ul className="sai-bars mt-2">
          {rows.map((row, index) => (
            <li key={row.label} className="sai-bar-row">
              <span className="sai-bar-label">{row.label}</span>
              <span className="sai-bar-track">
                <span
                  className="sai-bar-fill"
                  data-kind={SENTIMENT_ROW_KINDS[index]}
                  style={{ width: `${row.pct}%` }}
                />
              </span>
              <span className="sai-bar-pct">{formatPercent(row.pct)}%</span>
              <span className="sai-bar-count">{nf(row.count)}</span>
            </li>
          ))}
        </ul>

        {/* Truthful progress (§21): real counts from this same snapshot;
            the run continues until the whole stored dataset is covered. */}
        <p className="mt-2 sai-mono text-[11px] text-slate-400">
          {nf(stats.analyzed)} / {nf(dataset.stored)} analyzed ·
          <span className="ml-1 inline-flex items-center gap-1 text-violet-200">
            <SparkleIcon className="sai-pulse-icon h-3 w-3" />{' '}
            {phase === 'TOPIC'
              ? 'discovering discussions…'
              : phase === 'INSIGHT'
                ? 'building audience insight…'
                : 'processing…'}
          </span>
        </p>
        <p className="mt-1 text-[11px] leading-relaxed text-slate-400">
          Initial results from analyzed comments - updating live until the full
          dataset finishes.
        </p>
      </SaiCard>
    </div>
  );
}

// Post-analysis composition lives in ./insights.tsx (Sprint 5.2 redesign,
// Sprint 9 split across sections): AudienceHero / SentimentDistributionCard /
// EmotionCard / MoodCard / IntensityCard / ConfidenceCard / CoverageCard /
// TechnicalDetails keep the same backend numbers in a friendlier L1→L3
// hierarchy - they are just distributed across the console sections now.

/** Router over the sentiment lifecycle (§18 states, §27 error/empty copy). */
function SentimentSection({
  state,
  onSentimentRetry,
}: Pick<OverlayBodyProps, 'state' | 'onSentimentRetry'>) {
  const sentiment = state.sentiment;

  if (sentiment.status === 'loading') return <AnalyzingView />;

  if (sentiment.status === 'error') {
    return (
      <SentimentErrorView
        errorCode={sentiment.errorCode}
        message={sentiment.errorMessage ?? "We couldn't retrieve the analysis. Try again."}
        onRetry={onSentimentRetry}
      />
    );
  }

  const data = sentiment.status === 'success' ? sentiment.data : null;
  if (data === null) {
    // Idle (not fetched yet, or the dev stub honestly reported unavailable).
    const acquisition = state.acquisition;
    const emptyDataset =
      acquisition.status === 'success' &&
      acquisition.data !== null &&
      acquisition.data.comments.status !== 'ok';
    return emptyDataset ? (
      <NoAudienceDataView />
    ) : (
      <AwaitingAnalysisView />
    );
  }

  switch (data.status) {
    case 'PROCESSING': {
      // Real progress only (§30): backend counts, remaining derived exactly.
      const remaining = Math.max(
        0,
        data.dataset.stored - data.dataset.analyzed - data.dataset.skipped - data.dataset.failed,
      );
      return (
        <AnalyzingView
          progress={
            remaining > 0
              ? {
                  stored: data.dataset.stored,
                  analyzed: data.dataset.analyzed,
                  remaining,
                }
              : undefined
          }
        />
      );
    }
    case 'FAILED':
      return (
        <SentimentErrorView
          errorCode={null}
          message="We couldn't retrieve the analysis. Try again."
          onRetry={onSentimentRetry}
        />
      );
    case 'NOT_ANALYZED':
      return data.stats.totalComments === 0 ? (
        <NoAudienceDataView />
      ) : (
        <AwaitingAnalysisView />
      );
    case 'PROCESSED':
    default:
      if (data.stats.totalComments === 0) return <NoAudienceDataView />;
      if (data.stats.analyzed === 0) {
        // PROCESSED with zero verdicts -> every row was language-skipped.
        return <InsufficientLanguageView data={data} />;
      }
      // Multi-section redesign: the Sentiment section owns the hero
      // distribution (big % + bar + count rows). The rest of the old L1→L3
      // stack moved to its own sections (hero → Overview, emotion/energy/
      // mood → Emotion, coverage/quality composed below by AcquiredView).
      return <SentimentDistributionCard data={data} />;
  }
}

/**
 * Single truthful dataset status (§24): derived exclusively from the
 * backend's sentiment lifecycle. It replaces the old static
 * "READY FOR AI ANALYSIS" row that could sit next to "ANALYSIS COMPLETE".
 */
function datasetStatus(state: OverlayState): { text: string; tone: string } {
  const sentiment = state.sentiment;
  if (sentiment.status === 'error') {
    return { text: 'Analysis unavailable', tone: 'text-amber-300' };
  }
  if (sentiment.status === 'loading') {
    return { text: 'Analysis running', tone: 'text-violet-300' };
  }
  const data = sentiment.status === 'success' ? sentiment.data : null;
  if (data === null) {
    return { text: 'Awaiting analysis', tone: 'text-slate-200' };
  }
  switch (data.status) {
    case 'PROCESSING':
      return { text: 'Analysis running', tone: 'text-violet-300' };
    case 'PROCESSED':
      return { text: 'Analysis complete', tone: 'text-emerald-300' };
    case 'FAILED':
      return { text: 'Analysis failed', tone: 'text-amber-300' };
    case 'NOT_ANALYZED':
    default:
      return { text: 'Awaiting analysis', tone: 'text-slate-200' };
  }
}

/**
 * Honest per-section placeholder (multi-section redesign §24): shown only
 * while the analysis has not produced final results yet - one quiet line of
 * state copy per section, unique wording, never a fabricated value.
 */
function SectionPending({ message }: { message: string }) {
  return (
    <div className="sai-section-pending">
      <p className="text-[11.5px] leading-relaxed text-slate-400">{message}</p>
    </div>
  );
}

/**
 * LOADING → SUCCESS: real acquisition facts + the sectioned intelligence
 * console (multi-section redesign). The old single vertical stack is split
 * into five focused sections behind the persistent nav - but the CONTENT is
 * unchanged: every section stays MOUNTED (inactive ones are hidden with
 * CSS only), so switching is instant, all data stays in memory, nothing is
 * ever refetched (§16/§34), and realtime/job updates keep rendering into
 * their sections without moving the user off the active one (§18).
 */
function AcquiredView({
  data,
  state,
  onSentimentRetry,
  onTopicsRetry,
  onInsightRetry,
  onSelectSection,
}: {
  data: VideoDataResponse;
  state: OverlayState;
  onSentimentRetry: () => void;
  onTopicsRetry: () => void;
  onInsightRetry: () => void;
  onSelectSection?: (section: SectionId) => void;
}) {
  const title = data.video.title || data.video.videoId;
  const comments = data.comments;
  const sentimentData = state.sentiment.status === 'success' ? state.sentiment.data : null;
  const dataset = sentimentData?.dataset ?? null;

  // Sprint 7 §21: insight evidence -> underlying topic row. The link target
  // auto-clears so a highlight never outlives its moment (and resets with
  // the video via the store's slice guards above).
  const [linkedTopicId, setLinkedTopicId] = useState<string | null>(null);
  useEffect(() => {
    if (linkedTopicId === null) return;
    const timer = setTimeout(() => setLinkedTopicId(null), 6000);
    return () => clearTimeout(timer);
  }, [linkedTopicId]);
  const selectSection = onSelectSection ?? ((): void => {});
  const onTopicSelect = (topicId: string): void => {
    setLinkedTopicId(topicId);
    // Cross-section link: jump to Topics so the highlighted row is visible.
    selectSection('topics');
  };

  // Truthful acquisition note (§14/§26): "more available" is only claimed
  // when the backend reported hasMore; the configured-limit wording is
  // only used when the backend also reported limitReached.
  let acquisitionNote: string | null = null;
  if (comments.status === 'ok') {
    if (comments.hasMore) {
      acquisitionNote = dataset?.limitReached
        ? `Acquisition limit reached - ${nf(comments.count)} collected, more comments may be available.`
        : 'More comments may be available from YouTube.';
    } else if (comments.count > 0) {
      acquisitionNote = 'All available comments acquired.';
    }
    if (acquisitionNote && data.source.cached) {
      acquisitionNote += ' Served from the acquisition cache.';
    }
  }
  const status = datasetStatus(state);

  // Frontend-only view state (§16): which section is on top. The content
  // itself is pure store data - switching never fetches or recomputes.
  const activeSection = state.activeSection;
  // Final result gates (identical to the old stack's):
  // - sentimentReady: the L1-L3 post-analysis cards (hero/coverage/quality)
  // - resultsFinal: strip, trend, topics and insight (PROCESSED + verdicts)
  const sentimentReady =
    sentimentData?.status === 'PROCESSED' &&
    sentimentData.stats.totalComments > 0 &&
    sentimentData.stats.analyzed > 0;
  const resultsFinal =
    sentimentData?.status === 'PROCESSED' && sentimentData.stats.analyzed > 0;

  const sectionClass = (id: SectionId): string =>
    `sai-section space-y-4${activeSection === id ? ' sai-section-active' : ''}`;
  const sectionProps = (id: SectionId, label: string) => ({
    'aria-label': label,
    'data-active': activeSection === id ? 'true' : 'false',
    className: sectionClass(id),
  });

  // Scroll back to the top of the body when the visible section changes, so
  // a long section never leaves the next one starting mid-scroll.
  const consoleRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const scroller = consoleRef.current?.closest('.sai-body');
    if (scroller instanceof HTMLElement) scroller.scrollTop = 0;
  }, [activeSection]);

  return (
    <div ref={consoleRef} className="sai-console space-y-4">
      {/* Persistent compact nav (§9): directly under the header, sticky at
          the top of the scroll area, never a website tab strip. */}
      <SectionNav active={activeSection} onSelect={selectSection} />

      {/* ── 1. OVERVIEW - "What is happening?" ───────────────────── */}
      <section {...sectionProps('overview', 'Overview')}>
        {/* L1 hero: big % + ring + friendly verdict (§6). */}
        {sentimentReady && <AudienceHero data={sentimentData} />}

        {/* §5 compact premium identity card: title + channel + a subtle
            liveness/status row. The video ID is deliberately quiet and
            lives in Technical Information instead of shouting. */}
        <section aria-label="Acquired video" className="sai-video-card">
          <div className="flex items-center gap-3">
            <span className="sai-video-mark" aria-hidden="true">
              <YoutubeIcon className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[12.5px] font-medium leading-snug break-words text-slate-50">
                {title}
              </p>
              {data.video.channelTitle && (
                <p className="mt-0.5 text-[11px] text-slate-400">
                  {data.video.channelTitle}
                </p>
              )}
            </div>
            {/* §53 subtle ownership marker: this intelligence belongs to
                the video currently open on YouTube - never a past one. */}
            <span className="sai-active-badge">ACTIVE VIDEO</span>
          </div>
          {/* One truthful dataset status (§24) with a subtle live dot (§5). */}
          <p className={`sai-video-status text-[11px] ${status.tone}`}>
            <span className="sai-live-dot" aria-hidden="true" />
            {status.text}
          </p>
        </section>

        {/* Quick MOOD / EMOTION / ENERGY glance cards → jump to Emotion. */}
        {sentimentReady && (
          <QuickGlanceCards data={sentimentData} onSelect={selectSection} />
        )}

        {/* Sprint 8 §16/§19: the live monitor strip - only renders once a
            REAL /realtime poll has landed (backend counts only, never
            client-derived movement). */}
        {resultsFinal && <RealtimeStrip state={state} />}

        {/* Quiet pointer while the analysis is still in flight - the full
            lifecycle states live in the Sentiment section. */}
        {!sentimentReady && (
          <button
            type="button"
            className="sai-section-jump sai-focus"
            onClick={() => selectSection('sentiment')}
          >
            Detailed analysis states live in the Sentiment section →
          </button>
        )}
      </section>

      {/* ── 2. SENTIMENT - distribution, movement, coverage, quality ─ */}
      <section {...sectionProps('sentiment', 'Sentiment analysis')}>
        {/* Sentiment lifecycle router: analyzing / awaiting / errors /
            final distribution - unchanged states, now section-scoped. */}
        <SentimentSection state={state} onSentimentRetry={onSentimentRetry} />

        {/* AUDIENCE MOVEMENT: the same backend realtime trend, as a card. */}
        {resultsFinal && <AudienceMovementCard state={state} />}

        {/* §12 compact coverage counters - collected / analyzed / skipped,
            skipped visually secondary, denominator always visible. */}
        {comments.status === 'ok' ? (
          <CoverageCard
            collected={comments.count}
            analyzed={sentimentData?.stats.analyzed ?? null}
            dataset={dataset}
            note={acquisitionNote}
            delay={460}
          />
        ) : (
          <CommentsUnavailableBlock status={comments.status} />
        )}

        {/* §11 analysis quality - small secondary indicator, honest
            "sentiment decision margin" wording (never "accuracy"). */}
        {sentimentReady && <ConfidenceCard data={sentimentData} delay={520} />}
      </section>

      {/* ── 3. EMOTION - "What is the audience feeling?" ────────── */}
      <section {...sectionProps('emotion', 'Emotion intelligence')}>
        {!sentimentReady ? (
          <SectionPending message="Emotion, energy and mood appear here when the analysis completes." />
        ) : sentimentData.emotion ||
          sentimentData.intensity ||
          sentimentData.audienceMood ? (
          <>
            {/* Premium dominant-emotion card (§8). */}
            <EmotionCard data={sentimentData} delay={80} />
            {/* Emotional energy spectrum (§10). */}
            <IntensityCard data={sentimentData} delay={300} />
            {/* Supporting audience mood (§9). */}
            <MoodCard data={sentimentData} delay={140} />
          </>
        ) : (
          // Legacy body: blocks absent → honest absence, never invented.
          <SectionPending message="No emotion breakdown was provided by this analysis." />
        )}
      </section>

      {/* ── 4. TOPICS - discussion intelligence (Sprint 6) ───────── */}
      <section {...sectionProps('topics', 'Discussion topics')}>
        {resultsFinal ? (
          <TopicSection
            state={state}
            onRetry={onTopicsRetry}
            highlightTopicId={linkedTopicId}
          />
        ) : (
          <SectionPending message="Discussion themes appear here when the analysis completes." />
        )}
      </section>

      {/* ── 5. AI INSIGHT - the briefing (Sprint 7 §42 bridge) ───── */}
      <section {...sectionProps('insight', 'AI audience insight')}>
        {resultsFinal ? (
          <InsightSection
            state={state}
            onRetry={onInsightRetry}
            onTopicSelect={onTopicSelect}
          />
        ) : (
          <SectionPending message="The audience briefing appears here when the analysis completes." />
        )}
      </section>

      {/* §13 progressive disclosure as a SECONDARY control (never a nav
          item): source, cache, model terminology and timestamps stay one
          click away and never dominate any section. */}
      <div className="sai-tech-control">
        <TechnicalDetails
          source={data.source}
          videoId={data.video.videoId}
          pageKind={state.videoContext?.pageKind ?? 'watch'}
          retrievedAt={data.source.retrievedAt}
          insight={state.insight.status === 'success' ? state.insight.data : null}
        />
      </div>
    </div>
  );
}

function ReadyView({ state }: Pick<OverlayBodyProps, 'state'>) {
  const context = state.videoContext;
  if (!context?.videoId) return null;
  return (
    <div className="space-y-4">
      <section aria-label="Current video">
        <div className="flex items-center justify-between gap-2">
          <h2 className="sai-hud">Current Video</h2>
          <span className="sai-active-badge">ACTIVE VIDEO</span>
        </div>
        <dl className="sai-inset mt-2 space-y-1.5 rounded-xl px-3 py-2.5">
          <div className="flex items-center justify-between gap-3">
            <dt className="sai-key">ID</dt>
            <dd className="sai-mono truncate">{context.videoId}</dd>
          </div>
          <div className="flex items-center justify-between gap-3">
            <dt className="sai-key">Page</dt>
            <dd className="sai-value">{context.pageKind}</dd>
          </div>
          <div className="flex items-center justify-between gap-3">
            <dt className="sai-key">Status</dt>
            <dd className="sai-value">{STATUS_LABELS[state.status]}</dd>
          </div>
        </dl>
      </section>

      {/* §20 initial overlay: no action anywhere - the automatic flow
          takes over the moment the video is detected. */}
      <section aria-label="Audience acquisition" className="sai-inset space-y-2.5 rounded-xl px-3.5 py-3">
        <p className="sai-analysis-state">DETECTING AUDIENCE RESPONSE</p>
        <div className="flex items-center gap-2.5">
          <div className="sai-spinner" aria-hidden="true" />
          <p className="text-[12.5px] leading-relaxed text-slate-200">
            Preparing the automatic analysis for this video…
          </p>
        </div>
        <p className="text-[11.5px] leading-relaxed text-slate-400">
          Comments are collected and analyzed automatically - results appear here when the run
          completes. No button needed.
        </p>
      </section>
    </div>
  );
}

/**
 * Body state router (Sprint 4): renders honest acquisition states first,
 * then the sentiment intelligence layer behind them - every value shown is
 * backend-derived, never fabricated locally.
 */
export function OverlayBody({
  state,
  onRetry,
  onSentimentRetry,
  onTopicsRetry,
  onInsightRetry,
  onSelectSection,
}: OverlayBodyProps) {
  const context = state.videoContext;
  const acquisition = state.acquisition;
  const jobState = state.job;
  const jobSnapshot = jobState.status === 'running' ? jobState.job : null;

  // Sprint 4.4 §19: CONNECTING precedes everything while the bounded
  // backend availability gate runs.
  if (state.status === 'connecting') {
    return <ConnectingView />;
  }

  if (state.status === 'error') {
    // Sprint 4.3 §25: background-job failures render the interrupted view
    // (real collected count + Retry Analysis); legacy failures keep the
    // categorized acquisition error view.
    if (jobState.status === 'error') {
      return <JobFailedView state={state} onRetry={onRetry} />;
    }
    return <AcquisitionErrorView state={state} onRetry={onRetry} />;
  }

  // Sprint 5.1 §8/§9: FIRST REAL INSIGHT while the job is still running -
  // the moment one batch has verdicts, the real aggregate renders with an
  // explicit ANALYZING state (interim data is never presented as final).
  // Fires in both job phases (acquisition + interim analysis overlap), and
  // disappears the instant the job reaches a terminal status, where the
  // regular completed-result flow takes over.
  const progressiveData =
    state.sentiment.status === 'success' ? state.sentiment.data : null;
  if (
    jobSnapshot !== null &&
    !TERMINAL_JOB_STATUSES.has(jobSnapshot.status) &&
    (state.status === 'loading' || state.status === 'analyzing') &&
    progressiveData !== null &&
    progressiveData.stats.analyzed > 0
  ) {
    return <ProgressiveInsightView data={progressiveData} phase={jobSnapshot.phase} />;
  }

  // Background job SENTIMENT phase (the dataset fetch happens only after
  // the job completes): real batch counts from the polled snapshots
  // (§25 ANALYZING - count-based progress, never a fabricated percentage).
  if (
    state.status === 'analyzing' &&
    acquisition.status !== 'success' &&
    jobSnapshot !== null
  ) {
    return (
      <AnalyzingView
        progress={{
          stored: jobSnapshot.stored,
          analyzed: jobSnapshot.analyzed,
          remaining: jobSnapshot.pending,
        }}
      />
    );
  }

  // Acquisition in flight (READY → LOADING); the sentiment request window
  // (`analyzing`) keeps the acquired facts on screen instead.
  if (state.status === 'loading' || acquisition.status === 'loading') {
    return <AcquiringView jobState={jobState} />;
  }

  if (!context) {
    return (
      <MessageView message="AI Analyzer could not initialize. Please reload the page." />
    );
  }

  // Defense guard: content script only mounts on YouTube, but state could hold
  // a foreign URL if architecture changes later.
  if (!isYouTubeUrl(context.url)) {
    return <MessageView title="NOT AVAILABLE" message="Available on supported YouTube pages." />;
  }

  if (!context.isSupported) {
    return (
      <MessageView
        title="NOT AVAILABLE"
        message="This page is not currently supported."
        hint="Open a supported YouTube video to analyze audience sentiment."
      />
    );
  }

  if (!context.videoId) {
    return <MessageView title="NOT AVAILABLE" message="Unable to identify the current video." />;
  }

  if (
    acquisition.status === 'success' &&
    acquisition.data &&
    acquisition.videoId === context.videoId
  ) {
  return (
    <AcquiredView
      data={acquisition.data}
      state={state}
      onSentimentRetry={onSentimentRetry}
      onTopicsRetry={onTopicsRetry}
      onInsightRetry={onInsightRetry}
      onSelectSection={onSelectSection}
    />
  );
  }

  return <ReadyView state={state} />;
}
