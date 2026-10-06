import { useEffect, useState } from 'react';
import type {
  InsightAnalysis,
  InsightCard,
  InsightCardCategory,
  InsightEvidenceLine,
  OverlayState,
} from '../../shared/types';
import { nf } from '../format';
import {
  AlertIcon,
  BoltIcon,
  BrainIcon,
  HeartIcon,
  SparkleIcon,
  TargetIcon,
} from './icons';
import { SaiCard } from './ui';

/**
 * Sprint 7 — AI Audience Insight (§18-§23).
 *
 * The section is the BRIDGE between the individual analytics and human
 * understanding (§42): it phrases the backend's structured evidence and
 * always exposes the evidence behind each claim (§20 "Why?" panels) with
 * links down to the underlying topic rows (§21).
 *
 * HONESTY RULES baked in here:
 * - `source` decides the badge: only `llm` may say "AI-generated"; the
 *   deterministic default and the LLM-failure fallback are labeled
 *   data-derived (§23/§45 - never claim AI generation falsely).
 * - Loading copy is real (no fake thinking/streaming, §22): the state
 *   only ever reflects the actual request/job state.
 * - INSUFFICIENT_DATA renders the backend's honest copy instead of a
 *   summary (§10/§31); a failed fetch keeps sentiment/topics intact and
 *   offers the ONLY manual action - Retry from failure (§29).
 * - "Based on N analyzed comments" uses the analyzed denominator (§11).
 *
 * The section renders nothing when its slice is absent (no transport),
 * so the overlay never fabricates a summary client-side (§5).
 */

type IconComponent = React.ComponentType<{ className?: string }>;

const CATEGORY_ICONS: Record<InsightCardCategory, IconComponent> = {
  OVERALL_REACTION: BrainIcon,
  WHAT_WORKED: HeartIcon,
  MAIN_DISCUSSION: BoltIcon,
  PAIN_POINT: AlertIcon,
  EMOTIONAL_SIGNAL: SparkleIcon,
  TAKEAWAY: TargetIcon,
};

/** Honest per-source badge copy (§23) - never overclaims. */
const SOURCE_BADGE: Record<InsightAnalysis['source'], string> = {
  llm: 'AI-generated',
  deterministic: 'Data-derived',
  fallback: 'Data-derived',
};

const SOURCE_HINT: Record<InsightAnalysis['source'], string> = {
  llm: 'Generated from analyzed audience data.',
  deterministic: 'Derived from analyzed audience data.',
  fallback: 'AI generation was unavailable - text derived from the analysis.',
};

/** §20: one inspectable evidence line; topic lines link to their row (§21). */
function EvidenceLine({
  line,
  onTopicSelect,
}: {
  line: InsightEvidenceLine;
  onTopicSelect: (topicId: string) => void;
}) {
  const clickable = line.topicId !== null;
  const content = (
    <>
      <span className="sai-insight-ev-label">{line.label}</span>
      <span className="sai-insight-ev-value">{line.value}</span>
      {line.detail !== null && (
        <span className="sai-insight-ev-detail">{line.detail}</span>
      )}
      {clickable && <span className="sai-insight-ev-link">view topic →</span>}
    </>
  );
  if (clickable) {
    return (
      <li>
        <button
          type="button"
          className="sai-insight-ev sai-insight-ev-click"
          onClick={() => onTopicSelect(line.topicId as string)}
        >
          {content}
        </button>
      </li>
    );
  }
  return (
    <li className="sai-insight-ev">{content}</li>
  );
}

function EvidenceList({
  lines,
  onTopicSelect,
}: {
  lines: InsightEvidenceLine[];
  onTopicSelect: (topicId: string) => void;
}) {
  if (lines.length === 0) return null;
  return (
    <ul className="sai-insight-ev-list" aria-label="Evidence">
      {lines.map((line, index) => (
        <EvidenceLine
          key={`${line.kind}-${line.label}-${index}`}
          line={line}
          onTopicSelect={onTopicSelect}
        />
      ))}
    </ul>
  );
}

/** One supporting card (§19): title + one-sentence body + optional Why?. */
function InsightCardRow({
  card,
  delay,
  onTopicSelect,
}: {
  card: InsightCard;
  delay: number;
  onTopicSelect: (topicId: string) => void;
}) {
  const [showWhy, setShowWhy] = useState(false);
  const Icon = CATEGORY_ICONS[card.category];
  return (
    <div
      className="sai-insight-card"
      data-category={card.category}
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="sai-insight-card-head">
        <Icon className="sai-insight-card-icon h-3.5 w-3.5" />
        <span className="sai-insight-card-title">{card.title}</span>
        {card.evidence.length > 0 && (
          <button
            type="button"
            className="sai-insight-why"
            aria-expanded={showWhy}
            onClick={() => setShowWhy((previous) => !previous)}
          >
            Why?
          </button>
        )}
      </div>
      <p className="sai-insight-card-body">{card.body}</p>
      {showWhy && <EvidenceList lines={card.evidence} onTopicSelect={onTopicSelect} />}
    </div>
  );
}

/** Loading (§30): real state only - copy reflects the job/request truth. */
function InsightLoading({ state }: { state: OverlayState }) {
  const job = state.job;
  const jobRunning =
    job.status === 'running' &&
    (job.job === null ||
      !['COMPLETED', 'FAILED', 'CANCELLED', 'STALE'].includes(job.job.status));
  const waiting = jobRunning;
  return (
    <div className="sai-topic-state" role="status" aria-live="polite">
      <p className="sai-analysis-state">
        <SparkleIcon className="sai-pulse-icon h-3.5 w-3.5" />
        {waiting ? 'PREPARING AUDIENCE INTELLIGENCE' : 'BUILDING AUDIENCE INSIGHT'}
      </p>
      <p className="sai-friendly">
        {waiting
          ? 'Preparing audience intelligence…'
          : 'Connecting the audience signals…'}
      </p>
      <p className="text-[11.5px] leading-relaxed text-slate-400">
        Phrasing the measured analysis into a short briefing.
      </p>
    </div>
  );
}

/** Failure (§30): friendly copy + the only manual action, Retry (§29). */
function InsightError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="sai-topic-state" role="status">
      <p className="sai-analysis-state">
        <AlertIcon className="h-3.5 w-3.5 text-amber-300/90" />
        AUDIENCE INSIGHT UNAVAILABLE
      </p>
      <p className="text-[11.5px] leading-relaxed text-slate-300">{message}</p>
      <p className="text-[11px] leading-relaxed text-slate-400">
        Sentiment and topic intelligence remain unaffected.
      </p>
      <button type="button" className="sai-btn" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/** INSUFFICIENT_DATA (§10): honest copy from the backend, no summary. */
function InsightNotice({ message }: { message: string }) {
  return (
    <div className="sai-topic-state" role="status">
      <p className="sai-analysis-state">AUDIENCE INSIGHT</p>
      <p className="text-[11.5px] leading-relaxed text-slate-300">{message}</p>
    </div>
  );
}

/**
 * Router over the Sprint 7 insight lifecycle + the READY composition:
 * hero (headline + summary + sample + Why?) then the supporting cards
 * (§17 keeps each body to one sentence; the hero summary is 1-3).
 */
export function InsightSection({
  state,
  onRetry,
  onTopicSelect,
}: {
  state: OverlayState;
  onRetry: () => void;
  onTopicSelect: (topicId: string) => void;
}) {
  const insight = state.insight;
  const [showWhy, setShowWhy] = useState(false);

  // Collapse the hero Why? whenever the slice changes (video switch) so a
  // previous video's expanded evidence can never linger.
  useEffect(() => {
    setShowWhy(false);
  }, [insight.videoId]);

  if (insight.status === 'loading') return <InsightLoading state={state} />;
  if (insight.status === 'error') {
    return (
      <InsightError
        message={insight.errorMessage ?? "We couldn't retrieve the audience insight."}
        onRetry={onRetry}
      />
    );
  }

  const data: InsightAnalysis | null = insight.data;
  if (data === null) return null;
  if (data.status === 'INSUFFICIENT_DATA') {
    return (
      <InsightNotice
        message={data.message ?? 'Not enough analyzed audience evidence.'}
      />
    );
  }

  const overall = data.cards.find((card) => card.category === 'OVERALL_REACTION');
  const supporting = data.cards.filter(
    (card) => card.category !== 'OVERALL_REACTION',
  );
  const heroEvidence = overall?.evidence ?? [];

  return (
    <SaiCard label="AI Audience Insight" className="sai-card-insight">
      <div className="sai-insight-head">
        <h2 className="sai-hud">
          <BrainIcon className="sai-insight-head-icon h-3.5 w-3.5" />
          AI Audience Insight
        </h2>
        <span className="sai-insight-badge" data-source={data.source}>
          {SOURCE_BADGE[data.source]}
        </span>
      </div>

      <p className="sai-insight-headline">{data.headline}</p>
      <p className="sai-insight-summary">{data.summary}</p>

      <div className="sai-insight-meta">
        <span>
          Based on {nf(data.sample.analyzed)} analyzed comments
          {data.sample.skipped > 0
            ? ` · ${nf(data.sample.skipped)} skipped`
            : ''}
        </span>
        <button
          type="button"
          className="sai-insight-why"
          aria-expanded={showWhy}
          disabled={heroEvidence.length === 0}
          onClick={() => setShowWhy((previous) => !previous)}
        >
          Why?
        </button>
      </div>
      <p className="sai-insight-source-hint">{SOURCE_HINT[data.source]}</p>

      {showWhy && <EvidenceList lines={heroEvidence} onTopicSelect={onTopicSelect} />}

      {supporting.map((card, index) => (
        <InsightCardRow
          key={card.category}
          card={card}
          delay={140 + index * 90}
          onTopicSelect={onTopicSelect}
        />
      ))}
    </SaiCard>
  );
}
