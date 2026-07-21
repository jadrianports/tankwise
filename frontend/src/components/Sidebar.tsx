import Box from '@mui/material/Box';

import PlannerFormSection from '../features/planner-form/PlannerFormSection';
import VehicleSection from '../features/vehicle/VehicleSection';
import ResultsSection from '../features/results/ResultsSection';
import RecentTripsSection from '../features/recent-trips/RecentTripsSection';

// Composes the sidebar's section slots in the locked D-18/D-20 content
// order. Each section is a self-contained feature component that reads
// shared solve state from useRoutePlanContext() -- this file never
// inlines a section's internals, so downstream plans replace each
// section's own file without ever touching Sidebar.tsx or App.tsx.
function Sidebar() {
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      <PlannerFormSection />
      <VehicleSection />
      <ResultsSection />
      <RecentTripsSection />
    </Box>
  );
}

export default Sidebar;
