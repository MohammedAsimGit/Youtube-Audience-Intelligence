import type { OverlayState, RealtimeStatus, RealtimeTrendState } from '../../shared/types';
import { SaiCard } from './ui';

/**
 * Sprint 8 — Realtime Audience Intelligence strip (§16 status indicators).
 *
 * HONESTY RULES baked in here:
 * - Every number is backend-computed (counts, trend, activity). This layer
 *   only formats - it never derives movement client-side (§4: no fabricated
 *   real-time behavior).
 * - Trend renders as ARROW + TEXT LABEL + exact delta, never color alone
 *   (§21 accessibility), and STABLE still shows its real delta so a
 *   below-the-floor movement stays inspectable.
 * - LIVE means a backend monitor thread is actually polling for THIS
 *   video; STANDBY / OFF are honest non-live states (§16).
 * - Relative timestamps are computed from the backend's own
 *   lastCheckedAt/lastUpdatedAt - nothing ticks on its own.
 *
 * Renders nothing until the first successful poll lands (absent, never
 * a placeholder that could be mistaken for live data).
 */

const TREND_META: Record<
  RealtimeTrendState,
  { arrow: string; label: string }
> = {
  RISING: { arrow: '▲', label: 'RISING' },
  FALLING: { arrow: '▼', label: 'FALLING' },
  STABLE: { arrow: '—', label: 'STABLE' },
};

function signed(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return `${rounded >= 0 ? '+' : ''}${rounded.toFixed(1)}`;
}

/** Honest relative time from a backend timestamp (null while unknown). */
function relativeSince(iso: string | null): string | null {
  if (iso === null) return null;
  const ms = Date.now() - Date.parse(iso);
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 5000) return 'just now';
  if (ms < 60_000) return `${Math.max(1, Math.round(ms / 1000))}s ago`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m ago`;
  return `${Math.round(ms / 3_600_000)}h ago`;
}

function livePill(data: RealtimeStatus): { label: string; live: boolean } {
  if (!data.enabled) return { label: 'LIVE UPDATES OFF', live: false };
  if (data.monitoring) return { label: 'LIVE', live: true };
  return { label: 'STANDBY', live: false };
}

export function RealtimeStrip({ state }: { state: OverlayState }) {
  const data = state.realtime.data;
  if (data === null) return null; // no successful poll yet - absent, not fake

  const pill = livePill(data);
  const trend = TREND_META[data.trend.state];
  const checked = relativeSince(data.lastCheckedAt);
  const activityWindow =
    data.activity.windowMinutes >= 1
      ? `${Math.round(data.activity.windowMinutes)}m`
      : `${Math.round(data.activity.windowMinutes * 60)}s`;

  return (
    <section aria-label="Realtime audience monitor" className="sai-realtime">
      <div className="sai-realtime-row">
        <span
          className={`sai-live-pill${pill.live ? ' is-live' : ''}`}
          data-testid="realtime-live-pill"
        >
          <span className="sai-live-dot" aria-hidden="true" />
          {pill.label}
        </span>

        {data.newComments > 0 && (
          <span className="sai-realtime-new" role="status">
            +{data.newComments} new
          </span>
        )}

        <span
          className={`sai-realtime-trend is-${data.trend.state.toLowerCase()}`}
          title={`Net sentiment movement since the previous update: ${signed(
            data.trend.changePp,
          )} percentage points`}
        >
          <span aria-hidden="true">{trend.arrow}</span> {trend.label}
          <span className="sai-realtime-delta">{signed(data.trend.changePp)}pp</span>
        </span>
      </div>

      <div className="sai-realtime-meta">
        <span className="sai-realtime-activity" title="Comments per minute over the trailing window">
          AUDIENCE {data.activity.level}
          <span className="sai-realtime-muted">
            {' '}
            · {data.activity.ratePerMinute.toFixed(1)}/min ({activityWindow})
          </span>
        </span>
        <span className="sai-realtime-muted">
          {checked !== null ? `checked ${checked}` : 'first check pending'}
        </span>
      </div>

      {data.pending > 0 && (
        <p className="sai-realtime-pending">
          {data.pending.toLocaleString()} new comment{data.pending === 1 ? '' : 's'} queued for
          analysis
        </p>
      )}
    </section>
  );
}

/**
 * Sentiment section trend card (redesign §22): the AUDIENCE MOVEMENT view
 * of the SAME backend `/realtime` snapshot that feeds the overview strip -
 * arrow + text label + exact delta, never a client-drawn graph and never a
 * movement derived outside the backend (§4). Renders only with a real poll
 * (absent, never a placeholder), gated by the caller on a final analysis.
 */
export function AudienceMovementCard({ state }: { state: OverlayState }) {
  const data = state.realtime.data;
  if (data === null) return null; // no successful poll yet - absent, not fake

  const trend = TREND_META[data.trend.state];
  return (
    <SaiCard label="Audience movement" className="sai-card-movement">
      <div className="sai-card-head">
        <span className="sai-card-title">AUDIENCE MOVEMENT</span>
        <span
          className={`sai-realtime-trend is-${data.trend.state.toLowerCase()}`}
          title={`Net sentiment movement since the previous update: ${signed(
            data.trend.changePp,
          )} percentage points`}
        >
          <span aria-hidden="true">{trend.arrow}</span> {trend.label}
          <span className="sai-realtime-delta">{signed(data.trend.changePp)}pp</span>
        </span>
      </div>
      <p className="sai-fineprint">
        Net sentiment movement since the previous analyzed-batch snapshot
        (percentage points).
      </p>
    </SaiCard>
  );
}
