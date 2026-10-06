import type { CSSProperties, ComponentType } from 'react';
import type {
  AudienceMood,
  DominantSentiment,
  InsightAnalysis,
  IntensityLevel,
  SectionId,
  SentimentAnalysis,
  VideoDataResponse,
} from '../../shared/types';
import { formatPercent, nf } from '../format';
import {
  AlertIcon,
  BoltIcon,
  BrainIcon,
  FrownIcon,
  GlobeIcon,
  HeartIcon,
  MehIcon,
  SmileIcon,
  SparkleIcon,
  TargetIcon,
  TrayIcon,
} from './icons';
import { Disclosure, InfoTip, SaiCard } from './ui';

/**
 * Sprint 5.2 — post-analysis intelligence components.
 *
 * Three-level information hierarchy (brief §3):
 *   L1 AudienceHero        - instant understanding (ring + headline)
 *   L2 emotion/mood/bars   - understand why (visual cards)
 *   L3 coverage/details    - explore the numbers (compact + expandable)
 *
 * DATA RULE (§27): every value renders straight from the backend body -
 * nothing is hardcoded, rounded differently, or invented client-side.
 * Optional blocks (emotion/intensity/confidence/mood) render only when the
 * backend sent them; a missing block hides exactly its own card.
 *
 * MOTION (§16/§29): one-shot CSS only - cards rise with a staggered
 * completion sequence, bars grow via scaleX, the ring fills via a keyframe.
 * All final values are in the DOM from the first paint (numbers are never
 * animated through fake intermediate states), and the global
 * `prefers-reduced-motion` rule in overlay.css neutralizes the rest.
 */

type IconComponent = ComponentType<{ className?: string }>;

const SENTIMENT_ICONS: Record<DominantSentiment, IconComponent> = {
  POSITIVE: SmileIcon,
  NEUTRAL: MehIcon,
  NEGATIVE: FrownIcon,
};

const SENTIMENT_WORDS: Record<DominantSentiment, string> = {
  POSITIVE: 'Positive',
  NEUTRAL: 'Neutral',
  NEGATIVE: 'Negative',
};

/**
 * Honest friendly headline: "Mostly X" only when X is a true majority
 * (>= 50%); below that the copy says X merely leads - never overstating.
 */
function headlineFor(dominant: DominantSentiment, pct: number): string {
  const word = SENTIMENT_WORDS[dominant];
  return pct >= 50 ? `Mostly ${word}` : `${word} Leads`;
}

function shareFor(stats: SentimentAnalysis['stats'], dominant: DominantSentiment | null): {
  pct: number;
  count: number;
} {
  if (dominant === 'POSITIVE') return { pct: stats.positivePercent, count: stats.positive };
  if (dominant === 'NEUTRAL') return { pct: stats.neutralPercent, count: stats.neutral };
  if (dominant === 'NEGATIVE') return { pct: stats.negativePercent, count: stats.negative };
  return { pct: 0, count: 0 };
}

// ---------------------------------------------------------------------------
// L1 — Audience Pulse hero
// ---------------------------------------------------------------------------

const RING_R = 52;
const RING_C = 2 * Math.PI * RING_R; // ~326.73

/**
 * The most important component in the overlay: animated radial sentiment
 * ring + friendly headline + real response count (§6).
 */
export function AudienceHero({ data }: { data: SentimentAnalysis }) {
  const { stats, dominantSentiment } = data;
  const share = shareFor(stats, dominantSentiment);
  const offset = RING_C * (1 - Math.min(100, Math.max(0, share.pct)) / 100);
  const Icon = dominantSentiment ? SENTIMENT_ICONS[dominantSentiment] : MehIcon;
  const headline = dominantSentiment ? headlineFor(dominantSentiment, share.pct) : '—';
  const ringVars = {
    '--sai-ring-full': RING_C,
    '--sai-ring-end': offset,
  } as CSSProperties;

  return (
    <SaiCard className="sai-hero" label="Overall audience reaction">
      <div className="sai-card-head">
        <span className="sai-card-title">
          <TargetIcon className="sai-card-icon" />
          Audience Reaction
        </span>
        <span className="sai-state-badge">ANALYSIS COMPLETE</span>
      </div>

      <div className="sai-hero-grid">
        <div className="sai-hero-ring">
          <svg viewBox="0 0 120 120" aria-hidden="true" focusable="false">
            <defs>
              <linearGradient id="sai-ring-gradient" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stopColor="#a78bfa" />
                <stop offset="55%" stopColor="#e879f9" />
                <stop offset="100%" stopColor="#22d3ee" />
              </linearGradient>
            </defs>
            <circle className="sai-ring-track" cx="60" cy="60" r={RING_R} />
            <circle
              className="sai-ring-fill"
              cx="60"
              cy="60"
              r={RING_R}
              strokeDasharray={RING_C}
              strokeDashoffset={offset}
              style={ringVars}
              transform="rotate(-90 60 60)"
            />
          </svg>
          <span className="sai-hero-pct" aria-hidden="true">
            {formatPercent(share.pct)}%
          </span>
        </div>

        <div className="sai-hero-copy">
          <p className="sai-hero-eyebrow">dominant sentiment</p>
          <p className="sai-hero-headline">
            <Icon className="sai-hero-face" />
            {headline}
          </p>
          <p className="sai-hero-responses">{nf(stats.analyzed)} audience responses</p>
          <p className="sai-live">
            <span className="sai-live-dot" aria-hidden="true" />
            Intelligence Active
          </p>
        </div>
      </div>
      {/* Screen-reader path: the same truthful values without the visuals.
          Wording deliberately differs from the coverage card's "Based on …" line
          so each fact is announced exactly once per view. */}
      <p className="sr-only">
        {dominantSentiment ?? 'No dominant sentiment'}: {formatPercent(share.pct)} percent,
        across {nf(stats.analyzed)} analyzed comments.
      </p>
    </SaiCard>
  );
}

// ---------------------------------------------------------------------------
// L2 — distribution / emotion / mood / intensity
// ---------------------------------------------------------------------------

const ROW_KINDS = ['positive', 'neutral', 'negative'] as const;
const ROW_ICONS: ReadonlyArray<IconComponent> = [SmileIcon, MehIcon, FrownIcon];

/** Visual interactive distribution: animated bars, icons, hover emphasis. */
export function SentimentDistributionCard({ data }: { data: SentimentAnalysis }) {
  const { stats, dominantSentiment } = data;
  const rows = [
    { label: 'Positive', count: stats.positive, pct: stats.positivePercent },
    { label: 'Neutral', count: stats.neutral, pct: stats.neutralPercent },
    { label: 'Negative', count: stats.negative, pct: stats.negativePercent },
  ];
  return (
    <SaiCard label="Sentiment distribution" delay={220}>
      <div className="sai-card-head">
        <span className="sai-card-title">Audience Sentiment</span>
        {/* Uppercase dominant verdict (the only all-caps value here). */}
        <span className="sai-state-badge">{dominantSentiment}</span>
      </div>

      <ul className="sai-bars">
        {rows.map((row, index) => {
          const RowIcon = ROW_ICONS[index];
          const isDominant =
            dominantSentiment === row.label.toUpperCase();
          return (
            <li
              key={row.label}
              className="sai-bar-row"
              data-dominant={isDominant ? 'true' : undefined}
            >
              <span className="sai-bar-label">
                <RowIcon className="sai-row-icon" />
                {row.label}
              </span>
              <span className="sai-bar-track">
                <span
                  className="sai-bar-fill"
                  data-kind={ROW_KINDS[index]}
                  style={{ width: `${row.pct}%` }}
                />
              </span>
              <span className="sai-bar-pct">{formatPercent(row.pct)}%</span>
              <span className="sai-bar-count">{nf(row.count)}</span>
            </li>
          );
        })}
      </ul>
      {/* §28 denominator lives on the coverage card only - one truthful
          "Based on … analyzed comments" sentence in the whole panel. */}
    </SaiCard>
  );
}

const INTENSITY_ROWS: ReadonlyArray<{
  level: IntensityLevel;
  label: string;
  kind: (typeof ROW_KINDS)[number];
}> = [
  { level: 'LOW', label: 'Low', kind: 'neutral' },
  { level: 'MEDIUM', label: 'Medium', kind: 'positive' },
  { level: 'HIGH', label: 'High', kind: 'negative' },
];

/** Dominant emotion card with an honest "Why" disclosure (§8). */
export function EmotionCard({
  data,
  delay,
}: {
  data: SentimentAnalysis;
  delay?: number;
}) {
  const emotion = data.emotion;
  if (!emotion) return null;
  const stats = data.stats;
  const label = emotion.dominant;
  const friendly =
    label === null || label === 'NEUTRAL'
      ? null
      : `${label.charAt(0)}${label.slice(1).toLowerCase()}`;
  return (
    <SaiCard label="Audience emotion" delay={delay}>
      <div className="sai-card-head">
        <span className="sai-card-title">
          <HeartIcon className="sai-card-icon sai-icon-emotion" />
          Audience Emotion
        </span>
      </div>
      <div className="sai-emotion-body">
        <span className="sai-emotion-label">{label ?? '—'}</span>
        <span className="sai-emotion-pct">{formatPercent(emotion.dominantPercent)}%</span>
      </div>
      <p className="sai-fineprint">of {nf(stats.analyzed)} analyzed comments</p>
      {friendly !== null && (
        <Disclosure title="Why this emotion?">
          <p className="sai-disclosure-copy">
            {friendly} was the most frequently detected emotion across analyzed comments.
          </p>
        </Disclosure>
      )}
    </SaiCard>
  );
}

const MOOD_COPY: Record<AudienceMood, string> = {
  POSITIVE: 'The audience response is generally positive.',
  CALM: 'The audience is engaged and relaxed.',
  EXCITED: 'The audience is enthusiastic and energized.',
  MIXED: 'The audience is divided in its reactions.',
  CONCERNED: 'The audience shows concern and skepticism.',
  NEGATIVE: 'The audience response is generally negative.',
};

/** Live-feeling mood signal with a pulse + friendly supporting copy (§9). */
export function MoodCard({
  data,
  delay,
}: {
  data: SentimentAnalysis;
  delay?: number;
}) {
  const mood = data.audienceMood;
  if (!mood) return null;
  return (
    <SaiCard label="Audience mood" delay={delay}>
      <div className="sai-card-head">
        <span className="sai-card-title">
          <SparkleIcon className="sai-card-icon sai-icon-mood" />
          Audience Mood
        </span>
        <InfoTip
          label="About audience mood"
          tip="derived from analyzed sentiment, emotion & intensity"
        />
      </div>
      <div className="sai-mood-body" data-mood={mood}>
        <span className="sai-mood-dot" aria-hidden="true" />
        <span className="sai-mood-value">{mood}</span>
      </div>
      <p className="sai-mood-copy">{MOOD_COPY[mood]}</p>
    </SaiCard>
  );
}

/** Emotional energy spectrum: overall band + segmented bar + band rows (§10). */
export function IntensityCard({
  data,
  delay,
}: {
  data: SentimentAnalysis;
  delay?: number;
}) {
  const intensity = data.intensity;
  if (!intensity) return null;
  const stats = data.stats;
  return (
    <SaiCard label="Emotional intensity" delay={delay}>
      <div className="sai-card-head">
        <span className="sai-card-title">
          <BoltIcon className="sai-card-icon sai-icon-energy" />
          Emotional Energy
        </span>
        <span className="sai-card-tools">
          {intensity.overall && <span className="sai-state-badge">{intensity.overall}</span>}
          <InfoTip
            label="About emotional intensity"
            tip="Measures how strongly emotional the analyzed audience responses are."
          />
        </span>
      </div>

      {/* Segmented spectrum (overall shape at a glance). */}
      <div
        className="sai-seg-track"
        role="img"
        aria-label={`Emotional energy: low ${formatPercent(intensity.distribution.LOW.percent)} percent, medium ${formatPercent(intensity.distribution.MEDIUM.percent)} percent, high ${formatPercent(intensity.distribution.HIGH.percent)} percent`}
      >
        <span
          className="sai-seg sai-seg-low"
          style={{ width: `${intensity.distribution.LOW.percent}%`, animationDelay: '160ms' }}
        />
        <span
          className="sai-seg sai-seg-medium"
          style={{ width: `${intensity.distribution.MEDIUM.percent}%`, animationDelay: '300ms' }}
        />
        <span
          className="sai-seg sai-seg-high"
          style={{ width: `${intensity.distribution.HIGH.percent}%`, animationDelay: '440ms' }}
        />
      </div>

      <ul className="sai-bars">
        {INTENSITY_ROWS.map((row) => {
          const share = intensity.distribution[row.level];
          return (
            <li key={row.level} className="sai-bar-row">
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
      <p className="sai-fineprint">
        {intensity.overall ? `Overall ${intensity.overall} · ` : ''}
        based on {nf(stats.analyzed)} analyzed comments
      </p>
    </SaiCard>
  );
}

/**
 * Secondary "analysis quality" card (§11): a SMALL circular indicator,
 * never equal weight to audience sentiment, and never called "accuracy" -
 * the caption keeps the honest decision-margin wording.
 */
export function ConfidenceCard({
  data,
  delay,
}: {
  data: SentimentAnalysis;
  delay?: number;
}) {
  const confidence = data.confidence;
  if (!confidence || confidence.average === null) return null;
  const pct = Math.round(confidence.average * 1000) / 10;
  const offset = 100 - Math.min(100, Math.max(0, pct));
  const miniC = 2 * Math.PI * 19;
  const miniVars = {
    '--sai-ring-full': miniC,
    '--sai-ring-end': miniC * (offset / 100),
  } as CSSProperties;
  return (
    <SaiCard label="Model confidence" delay={delay} className="sai-card-secondary">
      <div className="sai-card-head">
        <span className="sai-card-title">Analysis Quality</span>
        <InfoTip
          label="About model confidence"
          tip="Indicates how clearly the model distinguished the sentiment of analyzed comments."
        />
      </div>
      <div className="sai-confidence-body">
        <span className="sai-mini-ring" aria-hidden="true">
          <svg viewBox="0 0 48 48" focusable="false">
            <circle className="sai-ring-track" cx="24" cy="24" r="19" />
            <circle
              className="sai-ring-fill sai-ring-mini"
              cx="24"
              cy="24"
              r="19"
              strokeDasharray={miniC}
              strokeDashoffset={miniC * (offset / 100)}
              style={miniVars}
              transform="rotate(-90 24 24)"
            />
          </svg>
          <span className="sai-mini-pct">{formatPercent(pct)}%</span>
        </span>
        <div>
          <p className="sai-confidence-copy">
            {nf(data.stats.analyzed)} comments analyzed
          </p>
          <p className="sai-fineprint">sentiment decision margin</p>
          {/* The ring is aria-hidden, so announce the same value honestly. */}
          <span className="sr-only">{formatPercent(pct)}% confidence</span>
        </div>
      </div>
    </SaiCard>
  );
}

// ---------------------------------------------------------------------------
// L3 — coverage + technical details
// ---------------------------------------------------------------------------

/**
 * Compact data coverage card (§12): three visual counters with the skipped
 * count visually secondary, plus the always-visible denominator note.
 */
export function CoverageCard({
  collected,
  analyzed,
  dataset,
  note,
  delay,
}: {
  /** comments.count - the real collected total for this video. */
  collected: number;
  /** stats.analyzed when an analysis snapshot exists, else null. */
  analyzed: number | null;
  dataset: SentimentAnalysis['dataset'] | null;
  note?: string | null;
  delay?: number;
}) {
  const total = collected;
  return (
    <SaiCard label="Analysis coverage" delay={delay}>
      <div className="sai-card-head">
        <span className="sai-card-title">
          <TrayIcon className="sai-card-icon" />
          Analysis Coverage
        </span>
        <InfoTip
          label="About skipped comments"
          tip="Comments that could not be analyzed because their language is not currently supported."
        />
      </div>

      <ul className="sai-cov" aria-label="Dataset breakdown">
        <li className="sai-cov-item">
          <TrayIcon className="sai-cov-icon sai-icon-collect" />
          <span className="sai-cov-num">{nf(total)}</span>
          <span className="sai-cov-label">comments collected</span>
        </li>
        {analyzed !== null && (
          <li className="sai-cov-item">
            <BrainIcon className="sai-cov-icon sai-icon-analyzed" />
            <span className="sai-cov-num">{nf(analyzed)}</span>
            <span className="sai-cov-label">analyzed</span>
          </li>
        )}
        {dataset && dataset.skipped > 0 && (
          <li className="sai-cov-item sai-cov-secondary">
            <GlobeIcon className="sai-cov-icon" />
            <span className="sai-cov-num">{nf(dataset.skipped)}</span>
            <span className="sai-cov-label">skipped (language not supported)</span>
          </li>
        )}
        {dataset && dataset.failed > 0 && (
          <li className="sai-cov-item sai-cov-secondary">
            <AlertIcon className="sai-cov-icon sai-icon-failed" />
            <span className="sai-cov-num">{nf(dataset.failed)}</span>
            <span className="sai-cov-label">failed</span>
          </li>
        )}
      </ul>

      {analyzed !== null && analyzed > 0 && (
        <p className="sai-fineprint">Based on {nf(analyzed)} analyzed comments.</p>
      )}
      {note && <p className="sai-fineprint">{note}</p>}
    </SaiCard>
  );
}

/**
 * Technical information (§13): everything a curious user may want, hidden
 * behind a disclosure so it never dominates the first impression. Model
 * terminology (NRC emotion model, VADER) lives HERE, never on the cards.
 */
export function TechnicalDetails({
  source,
  videoId,
  pageKind,
  retrievedAt,
  insight = null,
}: {
  source: VideoDataResponse['source'];
  videoId: string;
  pageKind: string;
  retrievedAt: string;
  /** Sprint 7 §35: generation provenance lives HERE, never in the main UI. */
  insight?: InsightAnalysis | null;
}) {
  return (
    <Disclosure title="Technical Information">
      <dl className="sai-tech">
        <div className="sai-tech-row">
          <dt>Source</dt>
          <dd>YouTube{source.cached ? ' · cached' : ''}</dd>
        </div>
        <div className="sai-tech-row">
          <dt>Retrieved</dt>
          <dd>{retrievedAt}</dd>
        </div>
        <div className="sai-tech-row">
          <dt>Video</dt>
          <dd className="sai-mono">{videoId}</dd>
        </div>
        <div className="sai-tech-row">
          <dt>Page</dt>
          <dd>{pageKind}</dd>
        </div>
        <div className="sai-tech-row">
          <dt>Models</dt>
          <dd>VADER sentiment · NRC emotion model</dd>
        </div>
        {insight !== null && insight.status === 'READY' && (
          <>
            <div className="sai-tech-row">
              <dt>Insight provider</dt>
              <dd>
                {insight.provider.model !== null
                  ? `${insight.provider.name} · ${insight.provider.model}`
                  : insight.provider.name}
              </dd>
            </div>
            <div className="sai-tech-row">
              <dt>Insight source</dt>
              <dd>
                {insight.source === 'llm'
                  ? 'LLM-generated'
                  : insight.source === 'fallback'
                    ? 'deterministic fallback (LLM unavailable)'
                    : 'deterministic'}
              </dd>
            </div>
            <div className="sai-tech-row">
              <dt>Insight generated</dt>
              <dd>
                {insight.generatedAt ?? '—'}
                {insight.generationMs > 0 ? ` · ${insight.generationMs} ms` : ''}
              </dd>
            </div>
            <div className="sai-tech-row">
              <dt>Evidence version</dt>
              <dd className="sai-mono">
                {insight.evidenceVersion} · {nf(insight.sample.analyzed)} analyzed
              </dd>
            </div>
          </>
        )}
      </dl>
    </Disclosure>
  );
}

// Sprint 9 redesign: the L1→L3 stack is split across the console sections
// (hero → Overview, distribution/coverage/quality → Sentiment, emotion/
// energy/mood → Emotion) - see OverlayBody's AcquiredView composition.

// ---------------------------------------------------------------------------
// Overview quick cards (multi-section redesign §21)
// ---------------------------------------------------------------------------

/**
 * Compact MOOD / EMOTION / ENERGY glance cards for the Overview section.
 *
 * Same backend values as the full cards in the Emotion section - the
 * overview is a summary, the sections are the detail (only one is visible
 * at a time). Every tile is data-gated exactly like its full card: a
 * missing backend block hides exactly its own tile, and no value is ever
 * invented. Clicking a tile jumps to the Emotion section.
 */
export function QuickGlanceCards({
  data,
  onSelect,
}: {
  data: SentimentAnalysis;
  onSelect: (section: SectionId) => void;
}) {
  const { emotion, audienceMood, intensity } = data;
  const tiles: Array<{
    id: string;
    eyebrow: string;
    value: string;
    sub?: string;
    action: string;
  }> = [];
  if (audienceMood !== null && audienceMood !== undefined) {
    tiles.push({
      id: 'mood',
      eyebrow: 'MOOD',
      value: audienceMood,
      action: 'View audience mood details',
    });
  }
  if (emotion?.dominant) {
    tiles.push({
      id: 'emotion',
      eyebrow: 'EMOTION',
      value: emotion.dominant,
      sub: `${formatPercent(emotion.dominantPercent)}%`,
      action: 'View emotion details',
    });
  }
  if (intensity?.overall) {
    tiles.push({
      id: 'energy',
      eyebrow: 'ENERGY',
      value: intensity.overall,
      action: 'View emotional energy details',
    });
  }
  if (tiles.length === 0) return null;

  return (
    <div className="sai-quick-grid">
      {tiles.map((tile) => (
        <button
          key={tile.id}
          type="button"
          className="sai-quick sai-focus"
          aria-label={tile.action}
          onClick={() => onSelect('emotion')}
        >
          <span className="sai-quick-eyebrow">{tile.eyebrow}</span>
          <span className="sai-quick-value">{tile.value}</span>
          {tile.sub && <span className="sai-quick-sub">{tile.sub}</span>}
        </button>
      ))}
    </div>
  );
}
