import { useEffect, useRef, type ComponentType, type KeyboardEvent } from 'react';
import type { SectionId } from '../../shared/types';
import {
  BrainIcon,
  HeartIcon,
  MessageIcon,
  ScaleIcon,
  SparkleIcon,
} from './icons';

/**
 * Multi-section console navigation (redesign §6/§10).
 *
 * A persistent, compact bar that lives directly under the header (sticky at
 * the top of the scrollable body) - NOT a website tab strip: no big pills,
 * no wrapping. Each entry is a real <button> with a unique icon, a short
 * label, an active indicator line/glow, and hover/focus states.
 *
 * Interaction rules:
 * - Pure UI: selecting a section only flips `state.activeSection` in the
 *   store. Nothing is fetched, refetched or recomputed (§16/§34), so a
 *   realtime poll landing mid-switch can never move the user (§18).
 * - Keyboard: Arrow/Home/End move + activate, Tab still reaches every
 *   button, focus is always visible (`.sai-focus`).
 * - The strip never wraps: it scrolls horizontally (touch/trackpad safe,
 *   `overscroll-behavior-x: contain`) and never propagates a horizontal
 *   gesture to the page, so nav swipes can't dismiss the overlay (§12).
 */

type IconComponent = ComponentType<{ className?: string }>;

interface SectionSpec {
  id: SectionId;
  label: string;
  Icon: IconComponent;
}

/** Fixed order: overview is always the landing section. */
const SECTIONS: ReadonlyArray<SectionSpec> = [
  { id: 'overview', label: 'Overview', Icon: SparkleIcon },
  { id: 'sentiment', label: 'Sentiment', Icon: ScaleIcon },
  { id: 'emotion', label: 'Emotion', Icon: HeartIcon },
  { id: 'topics', label: 'Topics', Icon: MessageIcon },
  { id: 'insight', label: 'Insight', Icon: BrainIcon },
];

const IDS: ReadonlyArray<SectionId> = SECTIONS.map((section) => section.id);

interface SectionNavProps {
  active: SectionId;
  onSelect: (section: SectionId) => void;
}

export function SectionNav({ active, onSelect }: SectionNavProps) {
  const buttons = useRef<Partial<Record<SectionId, HTMLButtonElement | null>>>({});

  // Keep the active entry visible when the strip overflows (no wrap).
  useEffect(() => {
    buttons.current[active]?.scrollIntoView?.({
      block: 'nearest',
      inline: 'nearest',
    });
  }, [active]);

  const moveTo = (index: number): void => {
    const next = IDS[index];
    onSelect(next);
    buttons.current[next]?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const current = (event.target as HTMLElement).dataset.section as
      | SectionId
      | undefined;
    if (current === undefined) return;
    const index = IDS.indexOf(current);
    if (index < 0) return;
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      moveTo((index + 1) % IDS.length);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      moveTo((index - 1 + IDS.length) % IDS.length);
    } else if (event.key === 'Home') {
      event.preventDefault();
      moveTo(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      moveTo(IDS.length - 1);
    }
  };

  return (
    <nav className="sai-nav" aria-label="Intelligence sections">
      <div className="sai-nav-tabs" onKeyDown={onKeyDown}>
        {SECTIONS.map(({ id, label, Icon }) => {
          const isActive = active === id;
          return (
            <button
              key={id}
              type="button"
              ref={(element) => {
                buttons.current[id] = element;
              }}
              data-section={id}
              aria-current={isActive ? 'true' : undefined}
              className={`sai-nav-tab sai-focus${isActive ? ' is-active' : ''}`}
              onClick={() => onSelect(id)}
            >
              <Icon className="sai-nav-icon" />
              <span className="sai-nav-label">{label}</span>
              <span className="sai-nav-indicator" aria-hidden="true" />
            </button>
          );
        })}
      </div>
    </nav>
  );
}
