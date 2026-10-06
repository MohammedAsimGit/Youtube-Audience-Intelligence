import type { PageKind, VideoContext } from '../../shared/types';
import { isYouTubeUrl } from './detection';

const VIDEO_ID_PATTERN = /^[A-Za-z0-9_-]{11}$/;

function validateId(value: string | null): string | null {
  return value && VIDEO_ID_PATTERN.test(value) ? value : null;
}

function normalizePath(pathname: string): string {
  const trimmed = pathname.replace(/\/+$/, '');
  return trimmed === '' ? '/' : trimmed;
}

function segmentAfter(pathname: string, prefix: string): string | null {
  if (!pathname.startsWith(prefix)) return null;
  const [first] = pathname.slice(prefix.length).split('/');
  return first ? first : null;
}

function toPageKind(pathname: string): PageKind {
  if (pathname === '/') return 'home';
  if (pathname === '/watch') return 'watch';
  if (pathname.startsWith('/shorts/')) return 'shorts';
  if (pathname.startsWith('/live/')) return 'live';
  if (pathname.startsWith('/results')) return 'search';
  if (
    pathname.startsWith('/@') ||
    pathname.startsWith('/channel/') ||
    pathname.startsWith('/c/') ||
    pathname.startsWith('/user/')
  ) {
    return 'channel';
  }
  return 'other';
}

/**
 * Pure, testable core of video-context detection. The video id is always
 * parsed from the live URL - never hardcoded. Only locally-derivable
 * information is returned (url, id, page kind); real metadata belongs to
 * Sprint 2+.
 */
export function parseVideoContext(href: string, now: number = Date.now()): VideoContext {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return { url: href, videoId: null, pageKind: 'other', isSupported: false, detectedAt: now };
  }

  if (!isYouTubeUrl(url.href)) {
    return {
      url: url.href,
      videoId: null,
      pageKind: 'other',
      isSupported: false,
      detectedAt: now,
    };
  }

  const pathname = normalizePath(url.pathname);
  const pageKind = toPageKind(pathname);

  let videoId: string | null = null;
  if (pageKind === 'watch') {
    videoId = validateId(url.searchParams.get('v'));
  } else if (pageKind === 'shorts') {
    videoId = validateId(segmentAfter(pathname, '/shorts/'));
  } else if (pageKind === 'live') {
    videoId = validateId(segmentAfter(pathname, '/live/'));
  }

  const isSupported = pageKind === 'watch' || pageKind === 'shorts' || pageKind === 'live';
  return { url: url.href, videoId, pageKind, isSupported, detectedAt: now };
}

/** Reusable abstraction: identify the current YouTube video context. */
export function getCurrentVideoContext(): VideoContext {
  return parseVideoContext(window.location.href);
}
