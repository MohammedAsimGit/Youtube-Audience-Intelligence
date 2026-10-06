import { createRoot } from 'react-dom/client';
import type { AnalysisService } from '../services/analysis/analysis-service';
import type { AnalysisJobService } from '../services/analysis/analysis-job-service';
import type { SentimentService } from '../services/analysis/sentiment-service';
import type { InsightService } from '../services/analysis/insight-service';
import type { RealtimeService } from '../services/analysis/realtime-service';
import type { TopicsService } from '../services/analysis/topics-service';
import type { BackendGate } from '../services/backend/backend-gate';
import type { Store } from '../state/store';
import { App } from './App';

/** Mounts the React application into the (already isolated) container. */
export function mountApp(
  container: HTMLElement,
  store: Store,
  analysisService: AnalysisService,
  sentimentService?: SentimentService,
  jobService?: AnalysisJobService,
  topicsService?: TopicsService,
  insightService?: InsightService,
  realtimeService?: RealtimeService,
  backendGate?: BackendGate,
): () => void {
  const root = createRoot(container);
  root.render(
    <App
      store={store}
      analysisService={analysisService}
      sentimentService={sentimentService}
      jobService={jobService}
      topicsService={topicsService}
      insightService={insightService}
      realtimeService={realtimeService}
      backendGate={backendGate}
    />,
  );
  return () => root.unmount();
}
