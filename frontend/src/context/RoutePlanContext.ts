import { createContext, useContext } from 'react';

import type { RoutePlanError, RoutePlanStatus } from '../hooks/useRoutePlan';
import type { RouteResponse } from '../types/routeContract';

// Shared solve state (status/data/error) plus a `solve` handler, owned by
// App.tsx and read by Sidebar's section components -- downstream
// planner-form/vehicle/results plans consume this context instead of
// prop-drilling through Sidebar.tsx, and never need to edit App.tsx
// (09-03-PLAN.md Task 1). Split into its own file (rather than exported
// from App.tsx directly) so App.tsx's only runtime export stays the
// default component.
export interface RoutePlanContextValue {
  status: RoutePlanStatus;
  data: RouteResponse | null;
  error: RoutePlanError | null;
  solve: (start: string, finish: string) => Promise<void>;
}

export const RoutePlanContext = createContext<RoutePlanContextValue | null>(null);

export function useRoutePlanContext(): RoutePlanContextValue {
  const ctx = useContext(RoutePlanContext);
  if (!ctx) {
    throw new Error('useRoutePlanContext must be used within <App>');
  }
  return ctx;
}
