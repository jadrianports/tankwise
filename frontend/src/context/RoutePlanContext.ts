import { createContext, useContext } from 'react';

import type { RoutePlanError, RoutePlanStatus } from '../hooks/useRoutePlan';
import type { RouteResponse, VehicleProfileRequest } from '../types/routeContract';

// A StopList row click carries a `nonce` (not just the stop key) so MapView
// re-fires its fly-to/popup-open effect even when the same stop is clicked
// twice in a row -- a plain key-only value wouldn't change identity on a
// repeat click of the same stop.
export interface FocusStopRequest {
  key: string | number;
  nonce: number;
}

// Shared solve state (status/data/error) plus `solve`/`retry` handlers and
// the cross-pane `focusStop` bridge, owned by App.tsx and read by Sidebar's
// section components -- downstream planner-form/vehicle/results plans
// consume this context instead of prop-drilling through Sidebar.tsx, and
// never need to edit App.tsx (09-03-PLAN.md Task 1). Split into its own
// file (rather than exported from App.tsx directly) so App.tsx's only
// runtime export stays the default component.
//
// `focusStop` is how a sidebar StopList row (features/results) reaches the
// map pane (features/map/MapView.tsx) without either module importing the
// other directly: App.tsx owns the actual FocusStopRequest state and hands
// the setter down through this context, while MapView reads the resulting
// request as a plain prop and does the camera fly-to/popup-open itself
// (09-04-PLAN.md already built that half; this context only supplies the
// missing bridge).
export interface RoutePlanContextValue {
  status: RoutePlanStatus;
  data: RouteResponse | null;
  error: RoutePlanError | null;
  solve: (start: string, finish: string) => Promise<void>;
  retry: () => void;
  focusStop: (key: string | number) => void;
  // Vehicle preset/what-if slider bridge (UX-02/UX-12, D-07/D-14): updates
  // the vehicle profile used by the next solve and, if a route already
  // exists, immediately re-solves reusing the already-resolved
  // start/finish coordinates -- never re-geocodes. Consumed by
  // features/vehicle/useDebouncedResolve.ts, not called directly by
  // VehicleSection's chips/sliders.
  resolveVehicle: (vehicle: VehicleProfileRequest) => void;
}

export const RoutePlanContext = createContext<RoutePlanContextValue | null>(null);

export function useRoutePlanContext(): RoutePlanContextValue {
  const ctx = useContext(RoutePlanContext);
  if (!ctx) {
    throw new Error('useRoutePlanContext must be used within <App>');
  }
  return ctx;
}
