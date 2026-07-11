import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

// Pre-first-submit centered block, above the (empty) stop list area.
function EmptyState() {
  return (
    <Box sx={{ textAlign: 'center', py: 4 }}>
      <Typography variant="h6" component="h2" gutterBottom>
        Plan a route
      </Typography>
      <Typography variant="body1" color="text.secondary">
        Enter a start and finish location (an address or `lat,lng`), or pick a
        preset route below to see the cheapest fueling plan.
      </Typography>
    </Box>
  );
}

export default EmptyState;
