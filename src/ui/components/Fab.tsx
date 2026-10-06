import type { RefObject } from 'react';
import type { Store } from '../../state/store';
import { SparkleIcon } from './icons';

interface FabProps {
  store: Store;
  buttonRef: RefObject<HTMLButtonElement | null>;
}

/**
 * AI floating action button - the primary entry point. Independent from the
 * overlay lifecycle: closing the overlay re-renders this FAB, never unmounts
 * the extension. All visuals (gradient, glow, hover/active/focus feedback,
 * entrance animation) live in `.sai-fab` inside the isolated stylesheet.
 */
/**
 * §23 intelligence states: the FAB advertises what the backend is already
 * doing (idle → acquiring → analyzing → complete / error) via
 * `data-state`, derived purely from existing store state - no new polling,
 * no new job system, and the button's name/behavior stay identical.
 */
function deriveFabState(
  store: Store,
): 'idle' | 'acquiring' | 'analyzing' | 'complete' | 'error' {
  const state = store.getState();
  const job = state.job.status === 'running' ? state.job.job : null;
  const sentiment = state.sentiment;
  const data = sentiment.status === 'success' ? sentiment.data : null;
  if (
    state.status === 'error' ||
    state.job.status === 'error' ||
    state.acquisition.status === 'error' ||
    sentiment.status === 'error'
  ) {
    return 'error';
  }
  if (data !== null && data.status === 'PROCESSED') {
    return 'complete';
  }
  if (
    state.status === 'analyzing' ||
    sentiment.status === 'loading' ||
    (data !== null && data.status === 'PROCESSING') ||
    (job !== null && job.status === 'ANALYZING')
  ) {
    return 'analyzing';
  }
  if (
    state.status === 'loading' ||
    state.acquisition.status === 'loading' ||
    job !== null
  ) {
    return 'acquiring';
  }
  return 'idle';
}

export function Fab({ store, buttonRef }: FabProps) {
  return (
    <button
      type="button"
      ref={buttonRef}
      onClick={() => store.open()}
      aria-haspopup="dialog"
      aria-label="Open AI Analyzer"
      title="AI Analyzer"
      data-state={deriveFabState(store)}
      className="sai-fab fixed bottom-24 right-6 flex h-[58px] w-[58px] flex-col items-center justify-center gap-0.5"
    >
      <SparkleIcon className="h-4 w-4" />
      <span className="text-[9px] font-semibold tracking-[0.18em]">AI</span>
    </button>
  );
}
