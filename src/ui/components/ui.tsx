import { useId, useState, type CSSProperties, type ReactNode } from 'react';
import { ChevronDownIcon, InfoIcon } from './icons';

/**
 * Sprint 5.2 UI primitives - shared interaction building blocks for the
 * intelligence panel (progressive disclosure + compact tooltips + cards).
 * All animation is CSS-driven and one-shot; `prefers-reduced-motion`
 * neutralizes durations globally (see overlay.css).
 */

interface SaiCardProps {
  /** Accessible name for the card (role=region when provided). */
  label?: string;
  className?: string;
  /** Entrance stagger offset for the completion sequence (ms). */
  delay?: number;
  children: ReactNode;
}

/** Elevated surface card with a one-shot entrance animation. */
export function SaiCard({ label, className, delay, children }: SaiCardProps) {
  const style: CSSProperties | undefined =
    delay !== undefined ? { animationDelay: `${delay}ms` } : undefined;
  return (
    <section className={`sai-card${className ? ` ${className}` : ''}`} aria-label={label} style={style}>
      {children}
    </section>
  );
}

interface InfoTipProps {
  /** Tooltip copy (always present in the DOM - CSS toggles visibility). */
  tip: string;
  /** Accessible name of the trigger button (must be unique per view). */
  label: string;
}

/**
 * Compact information tooltip: hover OR keyboard focus reveals the copy,
 * so the explanation is available without a pointer (§20 accessibility).
 */
export function InfoTip({ tip, label }: InfoTipProps) {
  const id = useId();
  return (
    <span className="sai-tip">
      <button
        type="button"
        className="sai-tip-btn sai-focus"
        aria-label={label}
        aria-describedby={id}
      >
        <InfoIcon className="sai-tip-icon" />
      </button>
      <span className="sai-tip-bubble" role="tooltip" id={id}>
        {tip}
      </span>
    </span>
  );
}

interface DisclosureProps {
  title: string;
  /** Optional small right-aligned hint (counts, state). */
  hint?: string;
  children: ReactNode;
}

/**
 * Progressive-disclosure section (§13): collapsed by default, content is
 * unmounted while closed so the initial interface stays clean and cheap.
 * The trigger is a real button with `aria-expanded` (keyboard operable).
 */
export function Disclosure({ title, hint, children }: DisclosureProps) {
  const [open, setOpen] = useState(false);
  return (
    <section className="sai-disclosure">
      <button
        type="button"
        className="sai-disclosure-btn sai-focus"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronDownIcon
          className={`sai-disclosure-chevron${open ? ' sai-disclosure-chevron-open' : ''}`}
        />
        <span className="sai-disclosure-title">{title}</span>
        {hint && <span className="sai-disclosure-hint">{hint}</span>}
      </button>
      {open && <div className="sai-disclosure-panel">{children}</div>}
    </section>
  );
}
