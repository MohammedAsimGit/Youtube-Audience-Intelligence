/**
 * @vitest-environment jsdom
 * @vitest-environment-options { "url": "https://www.youtube.com/" }
 */
import { afterEach, describe, expect, it } from 'vitest';
import type { VideoContext } from '../../shared/types';
import { observeNavigation } from './spa-navigation';

/** YouTube SPA navigation: URL change + `yt-navigate-finish` event. */
function navigateTo(path: string, target: Document | Window = document): void {
  window.history.pushState({}, '', path);
  target.dispatchEvent(new Event('yt-navigate-finish'));
}

async function historyGo(direction: 'back' | 'forward'): Promise<void> {
  await new Promise<void>((resolve) => {
    window.addEventListener('popstate', () => resolve(), { once: true });
    window.history[direction]();
  });
}

describe('observeNavigation (YouTube SPA, Cases 1-5)', () => {
  const unsubscribers: Array<() => void> = [];

  function track(): VideoContext[] {
    const seen: VideoContext[] = [];
    unsubscribers.push(observeNavigation((context) => seen.push(context)));
    return seen;
  }

  afterEach(() => {
    while (unsubscribers.length > 0) {
      unsubscribers.pop()?.();
    }
  });

  it('fires only when the context changes and dedupes repeated events', () => {
    window.history.replaceState({}, '', '/watch?v=AAAAAAAAAAA');
    const seen = track();

    // Same navigation reported twice (document + window dispatch targets).
    navigateTo('/watch?v=BBBBBBBBBBB', document);
    navigateTo('/watch?v=BBBBBBBBBBB', window);
    // Re-emitted for the identical URL (SPA re-render) - no change.
    navigateTo('/watch?v=BBBBBBBBBBB', document);

    expect(seen).toHaveLength(1);
    expect(seen[0].videoId).toBe('BBBBBBBBBBB');
    expect(seen[0].pageKind).toBe('watch');
  });

  it('handles recommended video, search, back, forward, and rapid navigation', async () => {
    window.history.replaceState({}, '', '/watch?v=AAAAAAAAAAA');
    const seen = track();
    const ids = (): (string | null)[] => seen.map((context) => context.videoId);

    // Case 1 - recommended video: A → B.
    navigateTo('/watch?v=BBBBBBBBBBB');

    // Case 2 - YouTube search: video id becomes null on the results page.
    navigateTo('/results?search_query=cats');
    expect(seen[seen.length - 1].pageKind).toBe('search');
    expect(seen[seen.length - 1].videoId).toBeNull();

    // Case 3 - back navigation: search → B → A (not only forward movement).
    await historyGo('back');
    await historyGo('back');
    expect(ids()).toContain('AAAAAAAAAAA');

    // Case 4 - forward navigation: A → B again.
    await historyGo('forward');
    expect(ids()[ids().length - 1]).toBe('BBBBBBBBBBB');

    // Case 5 - rapid SPA navigation: A → B → C → D settles on D.
    navigateTo('/watch?v=CCCCCCCCCCC');
    navigateTo('/watch?v=DDDDDDDDDDD');
    expect(ids()[ids().length - 1]).toBe('DDDDDDDDDDD');

    // Every emitted context carried a parsed, valid identity.
    for (const context of seen) {
      expect(typeof context.url).toBe('string');
      expect(context.detectedAt).toBeGreaterThan(0);
    }
  });
});
