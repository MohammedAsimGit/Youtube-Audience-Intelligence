/**
 * Sprint 4.4 (§9–§15, §33–§35): backend availability gate.
 *
 * The extension can never execute shell commands (browser sandbox), so the
 * ONLY supported startup path is Chrome Native Messaging to a small local
 * host (launcher/native_host.py) with a strict closed protocol:
 *
 *     extension -> {"action": "ensure_backend"}
 *     host     -> {"ok": true, "outcome": "already_running" | "started"}
 *
 * Readiness is ALWAYS decided by the real `GET /health` endpoint (§35) -
 * never by "the port opened". Every wait is bounded (§15): a failed
 * startup resolves to a friendly `unavailable` result instead of retrying
 * forever. In development the backend is started manually (§13), so the
 * first health probe simply succeeds and Native Messaging is never used.
 */
import { logger } from '../../lib/logger';
import { getBackendOrigin } from '../analysis/http-analysis-service';

/** Host name registered by launcher/register_host.py (§33). */
export const NATIVE_HOST_NAME = 'com.sentiment_ai.backend';

/** User-facing copy for §30 - friendly, no connection/stack details. */
export const BACKEND_UNAVAILABLE_MESSAGE =
  'Local AI service is unavailable. Please restart the AI service and press Retry.';

export type BackendGateResult =
  | { kind: 'ready'; outcome: 'already_running' | 'started' }
  | {
      kind: 'unavailable';
      reason: 'health_failed' | 'native_failed' | 'native_unavailable' | 'timeout';
    };

/** The single seam the overlay depends on (easy to fake in tests, §40). */
export interface BackendGate {
  ensure(): Promise<BackendGateResult>;
}

/** Minimal structural type of chrome.runtime.Port (native messaging). */
export interface NativePortLike {
  postMessage(message: unknown): void;
  disconnect?(): void;
  onMessage: { addListener(listener: (message: unknown) => void): void };
  onDisconnect: { addListener(listener: () => void): void };
}

export interface NativeBackendGateOptions {
  origin?: string;
  fetchImpl?: typeof fetch;
  /** Returns null when Native Messaging is unavailable in this context. */
  connectNative?: (hostName: string) => NativePortLike | null;
  /** Bounded health attempts between probes (§15 - never infinite). */
  healthAttempts?: number;
  healthRetryDelayMs?: number;
  healthTimeoutMs?: number;
  /** Bounded wait for the native host (its own poll window is ~20 s). */
  nativeTimeoutMs?: number;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isHealthOkPayload(payload: unknown): boolean {
  return (
    typeof payload === 'object' &&
    payload !== null &&
    (payload as { status?: unknown }).status === 'ok'
  );
}

/** Chrome's native messaging entry point; null outside an extension. */
function defaultConnectNative(hostName: string): NativePortLike | null {
  const scope = globalThis as typeof globalThis & {
    chrome?: { runtime?: { connectNative?: (name: string) => NativePortLike } };
  };
  const connect = scope.chrome?.runtime?.connectNative;
  if (typeof connect !== 'function') return null;
  try {
    return connect.call(scope.chrome?.runtime, hostName);
  } catch {
    return null; // permission missing / host not registered (§30 friendly path)
  }
}

/**
 * Idempotent availability gate (§34): a healthy backend is reused without
 * spawning anything; `ensure()` calls made concurrently share ONE attempt.
 */
export class NativeBackendGate implements BackendGate {
  private readonly origin: string;
  private readonly fetchImpl: typeof fetch;
  private readonly connectNative: (hostName: string) => NativePortLike | null;
  private readonly healthAttempts: number;
  private readonly healthRetryDelayMs: number;
  private readonly healthTimeoutMs: number;
  private readonly nativeTimeoutMs: number;
  private inflight: Promise<BackendGateResult> | null = null;

  constructor(options: NativeBackendGateOptions = {}) {
    this.origin = options.origin ?? getBackendOrigin();
    this.fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args));
    this.connectNative = options.connectNative ?? defaultConnectNative;
    this.healthAttempts = options.healthAttempts ?? 6;
    this.healthRetryDelayMs = options.healthRetryDelayMs ?? 500;
    this.healthTimeoutMs = options.healthTimeoutMs ?? 1500;
    this.nativeTimeoutMs = options.nativeTimeoutMs ?? 25_000;
  }

  ensure(): Promise<BackendGateResult> {
    if (this.inflight === null) {
      this.inflight = this.run().finally(() => {
        this.inflight = null;
      });
    }
    return this.inflight;
  }

  private async run(): Promise<BackendGateResult> {
    // §34/§13: development mode - a manually started backend passes this
    // probe and the native host is never invoked.
    if (await this.probeHealth()) {
      return { kind: 'ready', outcome: 'already_running' };
    }

    const native = await this.askNativeHost();

    // §35: readiness comes from the health endpoint either way - even when
    // the host reports `started`, the port being open means nothing.
    if (await this.waitForHealth()) {
      return {
        kind: 'ready',
        outcome: native === 'ok' ? 'started' : 'already_running',
      };
    }

    const reason =
      native === 'timeout'
        ? 'timeout'
        : native === 'failed'
          ? 'native_failed'
          : native === 'unavailable'
            ? 'native_unavailable'
            : 'health_failed';
    logger.warn('backend gate unavailable', { reason });
    return { kind: 'unavailable', reason };
  }

  /** Real GET /health (§14) with a short bounded request timeout. */
  private async probeHealth(): Promise<boolean> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.healthTimeoutMs);
    try {
      const response = await this.fetchImpl(`${this.origin}/health`, {
        method: 'GET',
        cache: 'no-store',
        signal: controller.signal,
      });
      if (!response.ok) return false;
      const payload: unknown = await response.json();
      return isHealthOkPayload(payload);
    } catch {
      return false; // connection refused / aborted / non-JSON -> not healthy
    } finally {
      clearTimeout(timer);
    }
  }

  /** Bounded health polling (§15): never loops forever. */
  private async waitForHealth(): Promise<boolean> {
    for (let attempt = 0; attempt < this.healthAttempts; attempt += 1) {
      if (await this.probeHealth()) return true;
      if (attempt < this.healthAttempts - 1) {
        await delay(this.healthRetryDelayMs);
      }
    }
    return false;
  }

  /**
   * Strict closed protocol (§33): the ONLY message ever sent is
   * `{"action": "ensure_backend"}`. The host may be absent (not registered,
   * dev environment) - that is a bounded `unavailable`, never an error.
   */
  private askNativeHost(): Promise<'ok' | 'failed' | 'unavailable' | 'timeout'> {
    let port: NativePortLike | null;
    try {
      port = this.connectNative(NATIVE_HOST_NAME);
    } catch {
      return Promise.resolve('unavailable');
    }
    if (port === null) return Promise.resolve('unavailable');

    return new Promise((resolve) => {
      let settled = false;
      const finish = (outcome: 'ok' | 'failed' | 'timeout'): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(outcome);
      };
      const timer = setTimeout(() => {
        try {
          port?.disconnect?.();
        } catch {
          // Closing a dead port is harmless.
        }
        finish('timeout');
      }, this.nativeTimeoutMs);

      try {
        port.onMessage.addListener((message: unknown) => {
          const ok =
            typeof message === 'object' &&
            message !== null &&
            (message as { ok?: unknown }).ok === true;
          finish(ok ? 'ok' : 'failed');
        });
        port.onDisconnect.addListener(() => finish('failed')); // not registered
        port.postMessage({ action: 'ensure_backend' });
      } catch {
        finish('failed');
      }
    });
  }
}
