import { describe, expect, it } from 'vitest';
import { parseVideoContext } from './video-context';

describe('parseVideoContext', () => {
  it('extracts an 11-character id from a standard watch URL', () => {
    const ctx = parseVideoContext('https://www.youtube.com/watch?v=dQw4w9WgXcQ');
    expect(ctx.videoId).toBe('dQw4w9WgXcQ');
    expect(ctx.pageKind).toBe('watch');
    expect(ctx.isSupported).toBe(true);
    expect(ctx.url).toContain('/watch');
    expect(ctx.detectedAt).toBeGreaterThan(0);
  });

  it('keeps the id when extra parameters are present', () => {
    const ctx = parseVideoContext('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s');
    expect(ctx.videoId).toBe('dQw4w9WgXcQ');
  });

  it('rejects malformed ids (too short)', () => {
    const ctx = parseVideoContext('https://www.youtube.com/watch?v=short');
    expect(ctx.videoId).toBeNull();
    expect(ctx.pageKind).toBe('watch');
    expect(ctx.isSupported).toBe(true);
  });

  it('rejects malformed ids (illegal characters)', () => {
    const ctx = parseVideoContext('https://www.youtube.com/watch?v=abc%20defgh');
    expect(ctx.videoId).toBeNull();
  });

  it('classifies the homepage as unsupported home', () => {
    const ctx = parseVideoContext('https://www.youtube.com/');
    expect(ctx.pageKind).toBe('home');
    expect(ctx.isSupported).toBe(false);
    expect(ctx.videoId).toBeNull();
  });

  it('classifies search results as unsupported', () => {
    const ctx = parseVideoContext('https://www.youtube.com/results?search_query=cats');
    expect(ctx.pageKind).toBe('search');
    expect(ctx.isSupported).toBe(false);
  });

  it('classifies channel pages as unsupported', () => {
    const ctx = parseVideoContext('https://www.youtube.com/@somechannel');
    expect(ctx.pageKind).toBe('channel');
    expect(ctx.isSupported).toBe(false);
  });

  it('supports Shorts URLs', () => {
    const ctx = parseVideoContext('https://www.youtube.com/shorts/abc123DEF45');
    expect(ctx.pageKind).toBe('shorts');
    expect(ctx.videoId).toBe('abc123DEF45');
    expect(ctx.isSupported).toBe(true);
  });

  it('supports live URLs', () => {
    const ctx = parseVideoContext('https://www.youtube.com/live/abc123DEF45');
    expect(ctx.pageKind).toBe('live');
    expect(ctx.videoId).toBe('abc123DEF45');
    expect(ctx.isSupported).toBe(true);
  });

  it('treats non-YouTube URLs as unsupported other', () => {
    const ctx = parseVideoContext('https://example.com/watch?v=dQw4w9WgXcQ');
    expect(ctx.pageKind).toBe('other');
    expect(ctx.isSupported).toBe(false);
    expect(ctx.videoId).toBeNull();
  });

  it('treats look-alike hosts as non-YouTube', () => {
    const ctx = parseVideoContext('https://evil-youtube.com/watch?v=dQw4w9WgXcQ');
    expect(ctx.isSupported).toBe(false);
    expect(ctx.videoId).toBeNull();
  });

  it('survives garbage input without throwing', () => {
    const ctx = parseVideoContext('not a url');
    expect(ctx.pageKind).toBe('other');
    expect(ctx.isSupported).toBe(false);
    expect(ctx.videoId).toBeNull();
  });
});
