import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

// Placeholder slot -- the planner-form plan replaces this with the
// address autocomplete, swap/geolocate buttons and demo trip chips
// (UX-01/UX-05). This plan only establishes the Sidebar's locked section
// order and the shared solve-state context every section reads from
// (09-03-PLAN.md Task 1) -- deliberately no inlined form logic here.
function PlannerFormSection() {
  return (
    <Box>
      <Typography variant="h6" component="h2" gutterBottom>
        Plan your route
      </Typography>
      <Typography variant="body2" color="text.secondary">
        Start/finish inputs and demo trips land here in a later plan.
      </Typography>
    </Box>
  );
}

export default PlannerFormSection;
