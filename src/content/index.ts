import overlayCss from '../ui/styles/overlay.css?inline';
import { logger } from '../lib/logger';
import { isYouTubeUrl } from '../services/youtube/detection';
import { getCurrentVideoContext } from '../services/youtube/video-context';
import { observeNavigation } from '../services/youtube/spa-navigation';
import { HttpAnalysisService } from '../services/analysis/http-analysis-service';
import { HttpSentimentService } from '../services/analysis/http-sentiment-service';
import { HttpJobService } from '../services/analysis/http-job-service';
import { HttpTopicsService } from '../services/analysis/http-topics-service';
import { HttpInsightService } from '../services/analysis/http-insight-service';
import { HttpRealtimeService } from '../services/analysis/http-realtime-service';
import { NativeBackendGate } from '../services/backend/backend-gate';
import { createStore } from '../state/store';
import { mountApp } from '../ui/mount';

const HOST_ID = 'sentiment-ai-extension-root';

/**
 * Host lives in YouTube's document (not in the shadow tree), so its styles
 * must be inline: fixed positioning, the single controlled z-index ceiling,
 * and pointer-events:none so YouTube remains fully interactive underneath.
 */
function styleHost(host: HTMLElement): void {
  host.style.position = 'fixed';
  host.style.top = '0';
  host.style.left = '0';
  host.style.width = '0';
  host.style.height = '0';
  host.style.zIndex = '2147483647';
  host.style.pointerEvents = 'none';
}

/**
 * Minimal plain-DOM error surface used when React initialization fails.
 * Shows only friendly copy - stack traces go to the console, never the UI.
 */
function mountFallback(): void {
  try {
    const host = document.createElement('div');
    host.id = HOST_ID;
    styleHost(host);
    const shadow = host.attachShadow({ mode: 'open' });

    const panel = document.createElement('div');
    panel.style.cssText =
      'position:fixed;top:72px;right:16px;pointer-events:auto;padding:14px 18px;' +
      'border-radius:14px;background:rgba(10,14,24,.92);border:1px solid rgba(151,176,255,.25);' +
      'color:#e8eefc;font:500 13px/1.5 Roboto,"Segoe UI",sans-serif;' +
      'box-shadow:0 18px 50px rgba(0,0,0,.5);';

    const title = document.createElement('div');
    title.textContent = 'AI ANALYZER';
    title.style.cssText =
      'font-size:10px;letter-spacing:.2em;color:#9db3d9;margin-bottom:6px;font-weight:600;';

    const message = document.createElement('p');
    message.textContent = 'AI Analyzer could not initialize. Please reload the page.';
    message.style.margin = '0';

    panel.append(title, message);
    shadow.append(panel);
    (document.body ?? document.documentElement).appendChild(host);
  } catch (fallbackError) {
    logger.error('fallback UI failed to mount', fallbackError);
  }
}

function init(): void {
  const { origin, pathname } = window.location;
  logger.debug('extension initialized', { origin, pathname });

  // Defense-in-depth: the manifest already restricts matches to YouTube.
  if (!isYouTubeUrl(window.location.href)) {
    logger.warn('not a supported YouTube page - skipping mount');
    return;
  }
  logger.info('YouTube detected', { origin });

  if (document.getElementById(HOST_ID)) {
    logger.debug('extension already mounted on this page');
    return;
  }

  try {
    const store = createStore();

    const initial = getCurrentVideoContext();
    store.setVideoContext(initial);
    logger.info('video detected', { pageKind: initial.pageKind, videoId: initial.videoId });

    // SPA navigation: update application state when the active video
    // changes. Sprint 4.4: the store auto-opens the overlay for a detected
    // video and the overlay's automatic flow starts the analysis - no
    // button, no click (§4/§17). Duplicate events for the same id are
    // deduped here (parsed context) and again in the store (§7).
    observeNavigation((context) => {
      store.setVideoContext(context);
      logger.info('video changed', { pageKind: context.pageKind, videoId: context.videoId });
    });

    // Isolated root: one host element in YouTube's DOM, all UI in its Shadow DOM.
    const host = document.createElement('div');
    host.id = HOST_ID;
    styleHost(host);
    const shadow = host.attachShadow({ mode: 'open' });

    const style = document.createElement('style');
    style.textContent = overlayCss;

    const root = document.createElement('div');
    root.className = 'sai-root';

    shadow.append(style, root);
    (document.body ?? document.documentElement).appendChild(host);

    // Real acquisition + sentiment paths: extension -> backend -> YouTube /
    // sentiment engine. Credentials (YouTube API key) live only on the
    // backend (never in this bundle); the extension only GETs results.
    // Sprint 4.3: analysis runs as a background job (POST + poll), so no
    // single request ever waits for the whole pipeline.
    // Sprint 4.4: the backend gate makes the local FastAPI service
    // available automatically (health check -> native-host ensure_backend
    // -> health re-check, all bounded) - no terminal, no manual start.
    // Sprint 8: the realtime GET keeps the backend monitor alive while
    // the user stays on the video (bounded status polling, §9).
    mountApp(
      root,
      store,
      new HttpAnalysisService(),
      new HttpSentimentService(),
      new HttpJobService(),
      new HttpTopicsService(),
      new HttpInsightService(),
      new HttpRealtimeService(),
      new NativeBackendGate(),
    );
    logger.info('overlay mounted', { hostId: HOST_ID });
  } catch (error) {
    logger.error('extension initialization failed', error);
    mountFallback();
  }
}

init();
