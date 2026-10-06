import { useEffect, useRef } from 'react';
import type { AnalysisService } from '../services/analysis/analysis-service';
import type { AnalysisJobService } from '../services/analysis/analysis-job-service';
import type { SentimentService } from '../services/analysis/sentiment-service';
import { UnconfiguredSentimentService } from '../services/analysis/sentiment-service';
import type { InsightService } from '../services/analysis/insight-service';
import type { RealtimeService } from '../services/analysis/realtime-service';
import type { TopicsService } from '../services/analysis/topics-service';
import type { BackendGate } from '../services/backend/backend-gate';
import type { Store } from '../state/store';
import { useAppState } from '../state/useAppState';
import { Fab } from './components/Fab';
import { Overlay } from './components/Overlay';

interface AppProps {
  store: Store;
  analysisService: AnalysisService;
  /**
   * Sprint 4 sentiment transport. Optional so tests and dev shells can omit
   * it: the fallback is the honest offline stub (no results are fabricated).
   * Production wiring passes {@link HttpSentimentService}.
   */
  sentimentService?: SentimentService;
  /**
   * Sprint 4.3 background-job transport (POST + poll). Optional: when
   * absent the overlay keeps the legacy synchronous flow, which existing
   * tests and dev shells rely on. Production passes {@link HttpJobService}.
   */
  jobService?: AnalysisJobService;
  /**
   * Sprint 6 topic transport. Optional: when absent the discussion
   * section stays hidden (no topics are ever fabricated). Production
   * passes {@link HttpTopicsService}.
   */
  topicsService?: TopicsService;
  /**
   * Sprint 7 insight transport. Optional: when absent the AI insight
   * section stays hidden (no summary text is fabricated client-side).
   * Production passes {@link HttpInsightService}.
   */
  insightService?: InsightService;
  /**
   * Sprint 8 realtime transport. Optional: without it no live strip is
   * rendered (nothing is fabricated client-side). Production passes
   * {@link HttpRealtimeService}.
   */
  realtimeService?: RealtimeService;
  /**
   * Sprint 4.4 backend availability gate (§15): health probe + native-host
   * ensure_backend, bounded. Optional: without it the flow assumes the
   * backend is reachable (dev shells + tests). Production passes
   * {@link NativeBackendGate}.
   */
  backendGate?: BackendGate;
}

const FALLBACK_SENTIMENT_SERVICE = new UnconfiguredSentimentService();

/**
 * Root of the extension UI. Reads application state only - no YouTube DOM
 * knowledge here (Detection → Context → State → UI separation).
 *
 * CLOSED ⇄ OPEN: when closed only the FAB renders; closing never destroys the
 * extension, the FAB simply reappears.
 */
export function App({
  store,
  analysisService,
  sentimentService,
  jobService,
  topicsService,
  insightService,
  realtimeService,
  backendGate,
}: AppProps) {
  const state = useAppState(store);
  const fabRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  const isOpen = state.status !== 'closed';

  // Return focus to the FAB after the overlay closes (keyboard continuity).
  useEffect(() => {
    if (isOpen) {
      wasOpen.current = true;
      return;
    }
    if (wasOpen.current) {
      wasOpen.current = false;
      fabRef.current?.focus();
    }
  }, [isOpen]);

  // Escape closes the overlay from anywhere on the page.
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') store.close();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [isOpen, store]);

  return isOpen ? (
    <Overlay
      store={store}
      state={state}
      analysisService={analysisService}
      sentimentService={sentimentService ?? FALLBACK_SENTIMENT_SERVICE}
      jobService={jobService}
      topicsService={topicsService}
      insightService={insightService}
      realtimeService={realtimeService}
      backendGate={backendGate}
    />
  ) : (
    <Fab store={store} buttonRef={fabRef} />
  );
}
