import { logger } from '../../lib/logger';
import type { VideoContext } from '../../shared/types';
import { getCurrentVideoContext } from './video-context';

export type NavigationListener = (context: VideoContext) => void;

/**
 * YouTube is a single-page application: changing videos does not reload the
 * document. Detection strategy (chosen for reliability at near-zero cost):
 *
 * - `yt-navigate-finish` - the event YouTube itself dispatches at the end of
 *   SPA navigations (listened on both document and window because dispatch
 *   target differs across page states).
 * - `popstate` / `hashchange` - browser history fallback.
 * - Deduplication by comparing the parsed context, so double-fired events are
 *   harmless.
 *
 * Deliberately NO polling and NO MutationObserver: events only run when a
 * navigation already happened (performance rule).
 */
export function observeNavigation(onChange: NavigationListener): () => void {
  let last = getCurrentVideoContext();

  const check = (source: string): void => {
    const next = getCurrentVideoContext();
    if (next.url === last.url && next.videoId === last.videoId) return;
    last = next;
    logger.debug(`SPA navigation detected (${source})`, {
      pageKind: next.pageKind,
      videoId: next.videoId,
    });
    onChange(next);
  };

  const onYouTubeNavigate = (): void => check('yt-navigate-finish');
  const onHistoryChange = (): void => check('history');

  document.addEventListener('yt-navigate-finish', onYouTubeNavigate);
  window.addEventListener('yt-navigate-finish', onYouTubeNavigate);
  window.addEventListener('popstate', onHistoryChange);
  window.addEventListener('hashchange', onHistoryChange);

  return () => {
    document.removeEventListener('yt-navigate-finish', onYouTubeNavigate);
    window.removeEventListener('yt-navigate-finish', onYouTubeNavigate);
    window.removeEventListener('popstate', onHistoryChange);
    window.removeEventListener('hashchange', onHistoryChange);
  };
}
