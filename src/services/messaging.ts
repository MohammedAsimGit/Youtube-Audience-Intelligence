import type { ExtensionMessage, ExtensionMessageResponse } from '../shared/types';

/**
 * Extension messaging boundary (architecture groundwork). Sprint 1 has no
 * background worker, so sends resolve to null ("no receiver"). A later sprint
 * activates this path without UI changes - the React UI never talks to
 * chrome.* directly.
 */
export async function sendToBackground(
  message: ExtensionMessage,
): Promise<ExtensionMessageResponse | null> {
  if (typeof chrome === 'undefined' || !chrome.runtime?.id) return null;
  try {
    return await chrome.runtime.sendMessage(message);
  } catch {
    // "Receiving end does not exist" while no background worker is registered.
    return null;
  }
}
