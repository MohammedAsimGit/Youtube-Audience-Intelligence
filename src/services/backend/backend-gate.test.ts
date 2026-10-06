// @vitest-environment jsdom
/**
 * Sprint 4.4 §40: backend availability gate.
 *
 * - Backend already running  -> reused, native host never touched (§34/§13)
 * - Backend not running      -> native `ensure_backend` -> health re-check
 * - Startup fails            -> friendly `unavailable`, BOUNDED retries (§15)
 * - Readiness = real GET /health, never "port open" (§35)
 * - Strict protocol: the only message ever sent is `{"action": ...}` (§33)
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  BACKEND_UNAVAILABLE_MESSAGE,
  NATIVE_HOST_NAME,
  NativeBackendGate,
  type NativePortLike,
} from './backend-gate';

const ORIGIN = 'http://127.0.0.1:8000';

function okResponse(): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ status: 'ok', version: '0.4.4' }),
  } as unknown as Response;
}

function failResponse(): Response {
  return {
    ok: false,
    status: 503,
    json: async () => ({ detail: 'starting' }),
  } as unknown as Response;
}

/** Scripted fetch: entries are consumed in order; the last one repeats. */
function scriptedFetch(script: Array<'ok' | 'down' | 'throw'>): {
  impl: typeof fetch;
  calls: string[];
} {
  const calls: string[] = [];
  const impl = (async (input: RequestInfo | URL) => {
    calls.push(String(input));
    const next = script.length > 1 ? script.shift()! : script[0];
    if (next === 'throw') throw new TypeError('fetch failed');
    return next === 'ok' ? okResponse() : failResponse();
  }) as typeof fetch;
  return { impl, calls };
}

interface FakePort extends NativePortLike {
  sent: unknown[];
  /** Simulate Chrome delivering a host response. */
  reply(message: unknown): void;
  /** Simulate chrome.runtime.lastError + onDisconnect (host missing). */
  disconnectHost(): void;
}

function fakePort(autoDisconnect = false): FakePort {
  const messageListeners: Array<(m: unknown) => void> = [];
  const disconnectListeners: Array<() => void> = [];
  const port: FakePort = {
    sent: [],
    postMessage(message: unknown) {
      port.sent.push(message);
      if (autoDisconnect) queueMicrotask(() => port.disconnectHost());
    },
    onMessage: {
      addListener: (listener) => messageListeners.push(listener),
    },
    onDisconnect: {
      addListener: (listener) => disconnectListeners.push(listener),
    },
    reply(message) {
      for (const listener of messageListeners) listener(message);
    },
    disconnectHost() {
      for (const listener of disconnectListeners) listener();
    },
  };
  return port;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('backend already running (§13 dev mode / §34 idempotent)', () => {
  it('reuses the healthy backend and never touches the native host', async () => {
    const { impl, calls } = scriptedFetch(['ok']);
    const connectNative = vi.fn(() => {
      throw new Error('must not be called');
    });
    const gate = new NativeBackendGate({
      origin: ORIGIN,
      fetchImpl: impl,
      connectNative,
    });

    const result = await gate.ensure();
    expect(result).toEqual({ kind: 'ready', outcome: 'already_running' });
    expect(calls).toEqual([`${ORIGIN}/health`]);
    expect(connectNative).not.toHaveBeenCalled();
  });
});

describe('backend not running (§15 availability flow)', () => {
  it('asks the native host then trusts the health endpoint (§33/§35)', async () => {
    const { impl, calls } = scriptedFetch(['throw', 'ok']);
    const port = fakePort();
    const connectNative = vi.fn(() => port);
    const gate = new NativeBackendGate({
      origin: ORIGIN,
      fetchImpl: impl,
      connectNative,
      healthAttempts: 3,
      healthRetryDelayMs: 1,
    });

    const promise = gate.ensure();
    // The strict closed protocol: exactly one well-formed action (§33).
    await vi.waitFor(() => expect(port.sent).toHaveLength(1));
    expect(port.sent[0]).toEqual({ action: 'ensure_backend' });
    expect(connectNative).toHaveBeenCalledWith(NATIVE_HOST_NAME);
    port.reply({ ok: true, outcome: 'started', port: 8000 });

    const result = await promise;
    expect(result).toEqual({ kind: 'ready', outcome: 'started' });
    // Readiness still came from GET /health after the host replied (§35).
    expect(calls.length).toBeGreaterThanOrEqual(2);
  });

  it('shares ONE attempt across concurrent ensure() calls (§34)', async () => {
    const { impl, calls } = scriptedFetch(['ok']);
    const gate = new NativeBackendGate({
      origin: ORIGIN,
      fetchImpl: impl,
      connectNative: vi.fn(() => fakePort()),
    });

    const [first, second] = [gate.ensure(), gate.ensure()];
    expect(second).toBe(first); // one in-flight attempt, never two
    const [a, b] = await Promise.all([first, second]);
    expect(a).toEqual(b);
    expect(calls).toHaveLength(1); // one health probe total
  });
});

describe('startup failure (§15 bounded, §30 friendly)', () => {
  it('reports unavailable when the host is not registered - bounded probes', async () => {
    const { impl, calls } = scriptedFetch(['throw']);
    const gate = new NativeBackendGate({
      origin: ORIGIN,
      fetchImpl: impl,
      connectNative: () => null, // Chrome: host missing -> null port
      healthAttempts: 3,
      healthRetryDelayMs: 1,
    });

    const result = await gate.ensure();
    expect(result).toEqual({ kind: 'unavailable', reason: 'native_unavailable' });
    expect(calls.length).toBeLessThanOrEqual(6); // bounded - never infinite (§15)
  });

  it('reports unavailable when the host disconnects immediately', async () => {
    const { impl } = scriptedFetch(['throw']);
    const port = fakePort(true);
    const gate = new NativeBackendGate({
      origin: ORIGIN,
      fetchImpl: impl,
      connectNative: () => port,
      healthAttempts: 2,
      healthRetryDelayMs: 1,
    });

    const result = await gate.ensure();
    expect(result).toEqual({ kind: 'unavailable', reason: 'native_failed' });
  });

  it('times out on a silent host instead of waiting forever (§15)', async () => {
    const { impl } = scriptedFetch(['throw']);
    const port = fakePort(); // never replies, never disconnects
    const gate = new NativeBackendGate({
      origin: ORIGIN,
      fetchImpl: impl,
      connectNative: () => port,
      healthAttempts: 2,
      healthRetryDelayMs: 1,
      nativeTimeoutMs: 20,
    });

    const result = await gate.ensure();
    expect(result).toEqual({ kind: 'unavailable', reason: 'timeout' });
  });

  it('§35: host says ok but health never passes -> NOT ready', async () => {
    const { impl } = scriptedFetch(['throw']);
    const port = fakePort();
    const gate = new NativeBackendGate({
      origin: ORIGIN,
      fetchImpl: impl,
      connectNative: () => port,
      healthAttempts: 2,
      healthRetryDelayMs: 1,
    });

    const promise = gate.ensure();
    await vi.waitFor(() => expect(port.sent).toHaveLength(1));
    port.reply({ ok: true, outcome: 'started' }); // port open ≠ healthy

    const result = await promise;
    expect(result).toEqual({ kind: 'unavailable', reason: 'health_failed' });
  });

  it('exposes friendly copy with no connection/stack details (§30)', () => {
    expect(BACKEND_UNAVAILABLE_MESSAGE).toMatch(/local ai service is unavailable/i);
    expect(BACKEND_UNAVAILABLE_MESSAGE).not.toMatch(/127\.0\.0\.1|:8000|traceback|refused/i);
  });
});
