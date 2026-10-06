/**
 * Deterministic display formatting shared by the overlay body and the
 * insight cards (Sprint 5.2 UI).
 *
 * formatPercent (§20 of the original contract): backend percentages are
 * multiples of 0.1 - whole numbers render as "64", fractional ones as
 * "32.5". Never re-rounded here; the backend is the single source of truth.
 */
export function formatPercent(value: number): string {
  return Number.isInteger(value) ? `${value}` : value.toFixed(1);
}

export const nf = (value: number): string => value.toLocaleString('en-US');
