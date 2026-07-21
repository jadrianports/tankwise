import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

// Placeholder slot -- a later plan replaces this with the last-5-trips
// localStorage list (UX-06/D-41). Deliberately minimal this plan
// (09-03-PLAN.md Task 1) -- see PlannerFormSection.tsx for the same note.
function RecentTripsSection() {
  return (
    <Box>
      <Typography variant="h6" component="h2" gutterBottom>
        Recent trips
      </Typography>
      <Typography variant="body2" color="text.secondary">
        Your last few trips will appear here in a later plan.
      </Typography>
    </Box>
  );
}

export default RecentTripsSection;
