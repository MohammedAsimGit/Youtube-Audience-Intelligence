import type { RefObject } from 'react';
import type { OverlayStatus } from '../../shared/types';
import { CloseIcon, ExpandIcon, MinusIcon, RefreshIcon, SparkleIcon } from './icons';
import { StatusChip } from './StatusChip';

interface OverlayHeaderProps {
  status: OverlayStatus;
  minimized: boolean;
  /** Active video id - shown in the collapsed pill (body is hidden there). */
  videoId: string | null;
  restoreRef: RefObject<HTMLButtonElement | null>;
  onMinimize: () => void;
  onRestore: () => void;
  onRefresh: () => void;
  onClose: () => void;
  refreshDisabled?: boolean;
}

interface IconButtonProps {
  label: string;
  onClick: () => void;
  buttonRef?: RefObject<HTMLButtonElement | null>;
  children: React.ReactNode;
}

function IconButton({ label, onClick, buttonRef, children }: IconButtonProps) {
  return (
    <button
      type="button"
      ref={buttonRef}
      aria-label={label}
      onClick={onClick}
      className="sai-focus inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-slate-300 transition hover:bg-white/10 hover:text-white"
    >
      {children}
    </button>
  );
}

/**
 * Header: AI identity + current state + controls. In minimized mode it
 * collapses into the compact pill (`✦ AI … READY … +`) without unmounting
 * the overlay component, so application state is preserved.
 */
export function OverlayHeader({
  status,
  minimized,
  videoId,
  restoreRef,
  onMinimize,
  onRestore,
  onRefresh,
  onClose,
  refreshDisabled = false,
}: OverlayHeaderProps) {
  if (minimized) {
    return (
      <header className="flex items-center gap-2.5 px-3 py-2">
        <SparkleIcon className="h-4 w-4 shrink-0 text-violet-300" />
        <span className="sai-mono whitespace-nowrap text-[11px] font-semibold tracking-[0.08em] text-slate-100">
          {videoId ?? 'AI'}
        </span>
        <StatusChip
          status={status}
          label={status === 'complete' ? 'DATA ACQUIRED' : undefined}
        />
        <div className="ml-auto flex items-center gap-1">
          <IconButton label="Restore AI Analyzer panel" onClick={onRestore} buttonRef={restoreRef}>
            <ExpandIcon className="h-3.5 w-3.5" />
          </IconButton>
          <IconButton label="Close AI Analyzer" onClick={onClose}>
            <CloseIcon className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      </header>
    );
  }

  return (
    <header className="sai-header border-b px-4 py-3">
      <div className="flex items-center gap-2.5">
        <SparkleIcon className="sai-title-icon h-4 w-4 shrink-0" />
        <div className="min-w-0 leading-tight">
          <span className="block whitespace-nowrap text-[11px] font-semibold tracking-[0.22em] text-slate-50">
            SENTIMENT INTELLIGENCE
          </span>
          <span className="sai-subtitle block whitespace-nowrap text-[9.5px] tracking-[0.14em]">
            YouTube • Audience Analysis
          </span>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <IconButton label="Refresh analysis" onClick={onRefresh}>
            <RefreshIcon className={`h-3.5 w-3.5 ${refreshDisabled ? 'opacity-50' : ''}`} />
          </IconButton>
          <IconButton label="Minimize AI Analyzer panel" onClick={onMinimize}>
            <MinusIcon className="h-3.5 w-3.5" />
          </IconButton>
          <IconButton label="Close AI Analyzer" onClick={onClose}>
            <CloseIcon className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      </div>
      <div className="mt-2">
        <StatusChip
          status={status}
          label={status === 'complete' ? 'DATA ACQUIRED' : undefined}
        />
      </div>
    </header>
  );
}
