import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

// Placeholder slot -- the vehicle plan replaces this with the preset
// chips and live what-if sliders (UX-02/UX-12). Deliberately minimal this
// plan (09-03-PLAN.md Task 1) -- see PlannerFormSection.tsx for the same
// note.
function VehicleSection() {
  return (
    <Box>
      <Typography variant="h6" component="h2" gutterBottom>
        Vehicle
      </Typography>
      <Typography variant="body2" color="text.secondary">
        Preset chips and what-if sliders land here in a later plan.
      </Typography>
    </Box>
  );
}

export default VehicleSection;
