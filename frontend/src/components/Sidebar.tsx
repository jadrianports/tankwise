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
//
// The `print-hide` marker (09-08's print.css) is the only hook a print
// stylesheet has for telling "form/vehicle/recent-trips chrome" apart from
// "the driver route sheet content" (SummaryCard/StopList/LegBreakdown
// inside ResultsSection, which prints as-is) without print.css reaching
// into any section's own internals -- a plain className, no logic change.
function Sidebar() {
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      <Box className="print-hide">
        <PlannerFormSection />
      </Box>
      <Box className="print-hide">
        <VehicleSection />
      </Box>
      <ResultsSection />
      <Box className="print-hide">
        <RecentTripsSection />
      </Box>
    </Box>
  );
}

export default Sidebar;
