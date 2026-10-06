type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const LEVEL_ORDER: Record<LogLevel, number> = {
  debug: 10,
  info: 20,
  warn: 30,
  error: 40,
};

/** Raise to 'warn' (or 'error') before production release to reduce console noise. */
const MIN_LEVEL: LogLevel = 'debug';
const PREFIX = '[sentiment-ai]';

function emit(level: LogLevel, message: string, detail?: unknown): void {
  if (LEVEL_ORDER[level] < LEVEL_ORDER[MIN_LEVEL]) return;
  const line = `${PREFIX} ${message}`;
  if (detail === undefined) {
    console[level](line);
  } else {
    console[level](line, detail);
  }
}

/**
 * Controlled development logging. Logged events: extension initialization,
 * YouTube detection, video detection, video changes, overlay mounting, and
 * state transitions (open/close/minimize). No personal information is logged -
 * only public page identity (URL path, video id).
 */
export const logger = {
  debug: (message: string, detail?: unknown) => emit('debug', message, detail),
  info: (message: string, detail?: unknown) => emit('info', message, detail),
  warn: (message: string, detail?: unknown) => emit('warn', message, detail),
  error: (message: string, detail?: unknown) => emit('error', message, detail),
};
