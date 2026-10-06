import { describe, expect, it } from 'vitest';
import { isYouTubeUrl } from './detection';

describe('isYouTubeUrl', () => {
  it('accepts www.youtube.com', () => {
    expect(isYouTubeUrl('https://www.youtube.com/watch?v=dQw4w9WgXcQ')).toBe(true);
  });

  it('accepts bare youtube.com', () => {
    expect(isYouTubeUrl('https://youtube.com/')).toBe(true);
  });

  it('accepts m.youtube.com', () => {
    expect(isYouTubeUrl('https://m.youtube.com/watch?v=dQw4w9WgXcQ')).toBe(true);
  });

  it('rejects look-alike domains', () => {
    expect(isYouTubeUrl('https://evil-youtube.com/watch?v=x')).toBe(false);
    expect(isYouTubeUrl('https://youtube.com.evil.example/')).toBe(false);
  });

  it('rejects unrelated hosts', () => {
    expect(isYouTubeUrl('https://example.com/')).toBe(false);
  });

  it('rejects non-https schemes', () => {
    expect(isYouTubeUrl('http://www.youtube.com/')).toBe(false);
    expect(isYouTubeUrl('view-source:https://www.youtube.com/')).toBe(false);
  });

  it('rejects garbage input without throwing', () => {
    expect(isYouTubeUrl('not a url')).toBe(false);
    expect(isYouTubeUrl('')).toBe(false);
  });
});
