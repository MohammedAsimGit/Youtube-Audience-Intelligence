import { useEffect, useState, useRef, type ComponentType } from 'react';
import type {
  DominantSentiment,
  OverlayState,
  TopicAnalysis,
  TopicItem,
} from '../../shared/types';
import { formatPercent, nf } from '../format';
import { SaiCard } from './ui';
import {
  AlertIcon,
  BoltIcon,
  ChevronDownIcon,
  HeartIcon,
  MessageIcon,
  ScaleIcon,
  SparkleIcon,
} from './icons';

/**
 * Sprint 6 (§30-§34): "What Are People Talking About?" - the discussion
 * intelligence section slotted between the sentiment stack and coverage.
 *
 * Every value rendered here comes from the backend's TopicAnalysis body
 * (real aggregations over stored verdicts); this component never derives
 * a percentage or invents copy beyond layout labels. Wording follows the
 * evidence principle (§20/§47): "appreciated" and "struggle with" always
 * carry their mention counts and ratios next to them - the UI states what
 * the analyzed audience repeatedly said, never an objective claim.
 *
 * Interaction: one inline-expanded card at a time (§34 - no navigation,
 * no raw comment wall §37). Animation is CSS-driven and one-shot;
 * `prefers-reduced-motion` neutralizes it globally (overlay.css).
 */

type IconComponent = ComponentType<{ className?: string }>;

export type TopicGroupKind = 'discussed' | 'loved' | 'pain' | 'mixed';

interface GroupSpec {
  kind: TopicGroupKind;
  title: string;
  icon: IconComponent;
  items: TopicItem[];
}

const GROUP_TITLES: Record<TopicGroupKind, string> = {
  discussed: 'Most Discussed',
  loved: 'What People Loved',
  pain: 'What People Struggle With',
  mixed: 'Mixed Discussions',
};

const GROUP_ICONS: Record<TopicGroupKind, IconComponent> = {
  discussed: BoltIcon,
  loved: HeartIcon,
  pain: AlertIcon,
  mixed: ScaleIcon,
};

function mentionText(mentions: number): string {
  return `${nf(mentions)} mention${mentions === 1 ? '' : 's'}`;
}

/** Honest per-group meta line (§31-§33): counts + the group's own ratio. */
function metaFor(kind: TopicGroupKind, topic: TopicItem): string {
  const positive = topic.sentiment.POSITIVE.percent;
  const negative = topic.sentiment.NEGATIVE.percent;
  switch (kind) {
    case 'loved':
      return `${mentionText(topic.mentions)} · ${formatPercent(positive)}% positive`;
    case 'pain':
      return `${mentionText(topic.mentions)} · ${formatPercent(negative)}% negative`;
    case 'mixed':
      return `${mentionText(topic.mentions)} · ${formatPercent(positive)}% positive / ${formatPercent(negative)}% negative`;
    case 'discussed':
    default:
      return `${mentionText(topic.mentions)} · ${formatPercent(topic.sharePercent)}% of analyzed`;
  }
}

const SENTIMENT_ROWS: ReadonlyArray<{
  label: string;
  kind: 'positive' | 'neutral' | 'negative';
  key: DominantSentiment;
}> = [
  { label: 'Positive', kind: 'positive', key: 'POSITIVE' },
  { label: 'Neutral', kind: 'neutral', key: 'NEUTRAL' },
  { label: 'Negative', kind: 'negative', key: 'NEGATIVE' },
];

/** Inline expansion (§34): mentions → sentiment → emotion → intensity. */
function TopicDetail({
  topic,
  analyzed,
}: {
  topic: TopicItem;
  analyzed: number;
}) {
  const emotion = topic.emotion;
  const intensity = topic.intensity;
  const ideas = topic.keyPhrases.filter(
    (phrase) => phrase.toLowerCase() !== topic.label.toLowerCase(),
  );
  return (
    <div className="sai-topic-detail">
      <p className="sai-topic-share">
        {mentionText(topic.mentions)} · {formatPercent(topic.sharePercent)}% of{' '}
        {nf(analyzed)} analyzed comments
      </p>

      {ideas.length > 0 && (
        <p className="sai-topic-ideas">
          <span className="sai-topic-ideas-key">Also said as</span>
          {ideas.map((phrase) => (
            <span key={phrase} className="sai-topic-chip">
              {phrase}
            </span>
          ))}
        </p>
      )}

      <ul className="sai-bars" aria-label={`Sentiment for ${topic.label}`}>
        {SENTIMENT_ROWS.map((row) => {
          const share = topic.sentiment[row.key];
          return (
            <li key={row.key} className="sai-bar-row">
              <span className="sai-bar-label">{row.label}</span>
              <span className="sai-bar-track">
                <span
                  className="sai-bar-fill"
                  data-kind={row.kind}
                  style={{ width: `${share.percent}%` }}
                />
              </span>
              <span className="sai-bar-pct">{formatPercent(share.percent)}%</span>
              <span className="sai-bar-count">{nf(share.count)}</span>
            </li>
          );
        })}
      </ul>

      <dl className="sai-topic-facts">
        {emotion?.dominant && (
          <div className="sai-topic-fact">
            <dt>Dominant emotion</dt>
            <dd>
              {emotion.dominant} · {formatPercent(emotion.dominantPercent)}%
            </dd>
          </div>
        )}
        {intensity?.overall && (
          <div className="sai-topic-fact">
            <dt>Intensity</dt>
            <dd>
              {intensity.overall} · {formatPercent(intensity.distribution.LOW.percent)}% low{' '}
              {formatPercent(intensity.distribution.MEDIUM.percent)}% medium{' '}
              {formatPercent(intensity.distribution.HIGH.percent)}% high
            </dd>
          </div>
        )}
        <div className="sai-topic-fact">
          <dt>Topic confidence</dt>
          <dd>{Math.round(topic.confidence * 100)}%</dd>
        </div>
      </dl>

      {/* §20: factual summary of the computed numbers, never an
          objective claim about the video. */}
      <p className="sai-topic-evidence">{topic.evidence}</p>
    </div>
  );
}

function TopicRow({
  topic,
  kind,
  rank,
  analyzed,
  maxMentions,
  expanded,
  highlight,
  onToggle,
  delay,
}: {
  topic: TopicItem;
  kind: TopicGroupKind;
  rank: string | null;
  analyzed: number;
  maxMentions: number;
  expanded: boolean;
  /** §21 insight -> data linking: this row is the target of an insight click. */
  highlight: boolean;
  onToggle: () => void;
  delay: number;
}) {
  // Relative frequency within the group (documented): the bar scales to
  // the group's largest topic; the exact counts are always in text.
  const width = Math.max(6, Math.round((topic.mentions / maxMentions) * 100));
  const positive = topic.sentiment.POSITIVE.percent;
  const neutral = topic.sentiment.NEUTRAL.percent;
  const negative = topic.sentiment.NEGATIVE.percent;
  return (
    <li
      className="sai-topic"
      data-kind={kind}
      data-topic-id={topic.topicId}
      data-expanded={expanded ? 'true' : 'false'}
      data-highlight={highlight ? 'true' : 'false'}
      style={{ animationDelay: `${delay}ms` }}
    >
      <button
        type="button"
        className="sai-topic-head"
        aria-expanded={expanded}
        onClick={onToggle}
      >
        {rank !== null && <span className="sai-topic-rank">{rank}</span>}
        <span className="sai-topic-text">
          <span className="sai-topic-label">{topic.label}</span>
          <span className="sai-topic-meta">{metaFor(kind, topic)}</span>
        </span>
        <ChevronDownIcon
          className={`sai-topic-chev${expanded ? ' sai-topic-chev-open' : ''} h-3.5 w-3.5`}
        />
      </button>

      {kind === 'mixed' ? (
        // Mixed topics show their real sentiment split as a stacked bar
        // (§15): positive / neutral / negative, widths from the backend.
        <span className="sai-topic-mixbar" aria-hidden="true">
          <span data-kind="positive" style={{ width: `${positive}%` }} />
          <span data-kind="neutral" style={{ width: `${neutral}%` }} />
          <span data-kind="negative" style={{ width: `${negative}%` }} />
        </span>
      ) : (
        <span className="sai-topic-track" aria-hidden="true">
          <span className="sai-topic-fill" data-kind={kind} style={{ width: `${width}%` }} />
        </span>
      )}

      {expanded && <TopicDetail topic={topic} analyzed={analyzed} />}
    </li>
  );
}

function TopicGroup({
  spec,
  analyzed,
  expandedId,
  highlightTopicId,
  onToggle,
  delay,
}: {
  spec: GroupSpec;
  analyzed: number;
  expandedId: string | null;
  highlightTopicId: string | null;
  onToggle: (topicId: string) => void;
  delay: number;
}) {
  if (spec.items.length === 0) return null; // never render empty groups
  const maxMentions = Math.max(...spec.items.map((item) => item.mentions));
  const Icon = spec.icon;
  return (
    <section className="sai-topic-group" aria-label={spec.title}>
      <h3 className="sai-topic-group-title">
        <Icon className="sai-topic-group-icon h-3.5 w-3.5" />
        {spec.title}
      </h3>
      <ul className="sai-topic-list">
        {spec.items.map((topic, index) => (
          <TopicRow
            key={topic.topicId}
            topic={topic}
            kind={spec.kind}
            rank={
              spec.kind === 'discussed' ? String(index + 1).padStart(2, '0') : null
            }
            analyzed={analyzed}
            maxMentions={maxMentions}
            expanded={expandedId === topic.topicId}
            highlight={highlightTopicId === topic.topicId}
            onToggle={() => onToggle(topic.topicId)}
            delay={delay + index * 60}
          />
        ))}
      </ul>
    </section>
  );
}

/** §36: real backend-driven loading copy while discovery runs server-side. */
export function TopicLoading() {
  return (
    <div className="sai-topic-state" role="status" aria-live="polite">
      <p className="sai-analysis-state">
        <SparkleIcon className="sai-pulse-icon h-3.5 w-3.5" />
        DISCOVERING DISCUSSIONS
      </p>
      <p className="sai-friendly">Discovering audience discussions…</p>
      <p className="text-[11.5px] leading-relaxed text-slate-400">
        Grouping repeated themes from the analyzed comments.
      </p>
    </div>
  );
}

/** §39: topic failure never touches the sentiment results above. */
export function TopicError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="sai-topic-state" role="status">
      <p className="sai-analysis-state">
        <AlertIcon className="h-3.5 w-3.5 text-amber-300/90" />
        AUDIENCE TOPICS UNAVAILABLE
      </p>
      <p className="text-[11.5px] leading-relaxed text-slate-300">{message}</p>
      <p className="text-[11px] leading-relaxed text-slate-400">
        Sentiment, emotion and intensity results above are unaffected.
      </p>
      <button type="button" className="sai-btn" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/** §38: honest empty / insufficient copy straight from the backend. */
export function TopicNotice({ message }: { message: string }) {
  return (
    <div className="sai-topic-state" role="status">
      <p className="sai-analysis-state">DISCUSSION THEMES</p>
      <p className="text-[11.5px] leading-relaxed text-slate-300">{message}</p>
    </div>
  );
}

/**
 * Router over the Sprint 6 topic lifecycle. Rendered by the overlay only
 * after the sentiment result is final for the active video; the store's
 * video guards make a stale Video A response impossible under Video B.
 */
export function TopicSection({
  state,
  onRetry,
  highlightTopicId = null,
}: {
  state: OverlayState;
  onRetry: () => void;
  /** §21 insight -> data linking: expand + scroll to this topic row. */
  highlightTopicId?: string | null;
}) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const topics = state.topics;

  // Sprint 7 §21: an insight evidence click targets its underlying topic
  // row - expand it and bring it into view (query scoped to this card so
  // the shadow-DOM boundary is never crossed).
  useEffect(() => {
    if (highlightTopicId === null) return;
    setExpandedId(highlightTopicId);
    // Attribute scan (no CSS.escape): works in shadow DOM, jsdom, browsers.
    const target = Array.from(
      rootRef.current?.querySelectorAll<HTMLElement>('[data-topic-id]') ?? [],
    ).find((element) => element.dataset.topicId === highlightTopicId);
    target?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }, [highlightTopicId]);

  if (topics.status === 'loading') return <TopicLoading />;
  if (topics.status === 'error') {
    return (
      <TopicError
        message={topics.errorMessage ?? "We couldn't retrieve audience topics."}
        onRetry={onRetry}
      />
    );
  }

  const data: TopicAnalysis | null = topics.data;
  if (data === null) return null;
  if (data.status === 'INSUFFICIENT_DATA' || data.topics.length === 0) {
    return (
      <TopicNotice
        message={data.message ?? 'Not enough repeated discussion yet.'}
      />
    );
  }

  const specs: GroupSpec[] = [
    { kind: 'discussed', title: GROUP_TITLES.discussed, icon: GROUP_ICONS.discussed, items: data.mostDiscussed },
    { kind: 'loved', title: GROUP_TITLES.loved, icon: GROUP_ICONS.loved, items: data.mostAppreciated },
    { kind: 'pain', title: GROUP_TITLES.pain, icon: GROUP_ICONS.pain, items: data.painPoints },
    { kind: 'mixed', title: GROUP_TITLES.mixed, icon: GROUP_ICONS.mixed, items: data.mixedTopics },
  ];

  const toggle = (topicId: string): void =>
    setExpandedId((previous) => (previous === topicId ? null : topicId));

  return (
    <div ref={rootRef}>
    <SaiCard label="What are people talking about" className="sai-card-topics">
      <div className="sai-topic-heading">
        <h2 className="sai-hud">
          <MessageIcon className="sai-topic-heading-icon h-3.5 w-3.5" />
          What People Talk About
        </h2>
        <p className="sai-topic-subtitle">
          Discovered from {nf(data.analyzedComments)} analyzed comments
        </p>
      </div>

      {specs.map((spec, index) => (
        <TopicGroup
          key={spec.kind}
          spec={spec}
          analyzed={data.analyzedComments}
          expandedId={expandedId}
          highlightTopicId={highlightTopicId}
          onToggle={toggle}
          delay={140 + index * 90}
        />
      ))}

      {/* §33 honest empty state: the backend lists pain points only above
          the evidence threshold - when none arrive, say exactly that (state
          copy, never a fabricated concern). */}
      {data.painPoints.length === 0 && (
        <p className="sai-fineprint sai-topic-empty">
          No recurring negative theme reached the evidence threshold.
        </p>
      )}
    </SaiCard>
    </div>
  );
}
