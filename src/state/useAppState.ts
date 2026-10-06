import { useSyncExternalStore } from 'react';
import type { OverlayState } from '../shared/types';
import type { Store } from './store';

/** Subscribes a component to the central store without prop drilling context. */
export function useAppState(store: Store): OverlayState {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}
