const SUPPORTED_HOSTS = new Set([
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
]);

/**
 * YouTube detection service (defense-in-depth).
 *
 * The manifest already restricts injection to youtube.com / www.youtube.com;
 * this check guards runtime use (and tests) against ever treating a non-YouTube
 * URL as supported. Detection logic lives here only - never in UI components.
 */
export function isYouTubeUrl(href: string): boolean {
  try {
    const url = new URL(href);
    return url.protocol === 'https:' && SUPPORTED_HOSTS.has(url.hostname);
  } catch {
    return false;
  }
}
