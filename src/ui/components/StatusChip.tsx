import type { OverlayStatus } from '../../shared/types';

export const STATUS_LABELS: Record<OverlayStatus, string> = {
  closed: 'CLOSED',
  open: 'OPEN',
  ready: 'READY',
  connecting: 'CONNECTING',
  loading: 'LOADING',
  analyzing: 'ANALYZING',
  complete: 'COMPLETE',
  error: 'ERROR',
};

/**
 * HUD-style pill communicating the current overlay state.
 * `label` allows a friendlier wording for internal states (e.g. internal
 * `complete` renders as "DATA ACQUIRED").
 */
export function StatusChip({ status, label }: { status: OverlayStatus; label?: string }) {
  return (
    <span data-status={status} className="sai-chip">
      {label ?? STATUS_LABELS[status]}
    </span>
  );
}
