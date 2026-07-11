import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Typography from '@mui/material/Typography';
import Box from '@mui/material/Box';

import { formatGallons, formatMiles } from '../utils/format';

// Sidebar summary card: the hero total-fuel-cost figure (Display role, Fuel
// Amber accent) plus total gallons, total route miles, and stop count
// (Body/Label roles). Rendered only on a successful plan.
function SummaryCard({ data }) {
  const stopCount = data.fuel_stops?.length ?? 0;

  return (
    <Card variant="outlined">
      <CardContent>
        <Typography variant="body2" color="text.secondary">
          Total fuel cost
        </Typography>
        <Typography variant="h4" component="p" sx={{ color: 'fuel.main', mb: 1 }}>
          ${data.total_cost}
        </Typography>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
          <Typography variant="body1">{formatGallons(data.total_gallons)} total</Typography>
          <Typography variant="body1">{formatMiles(data.total_route_mi)} route</Typography>
          <Typography variant="body1">
            {stopCount} fuel {stopCount === 1 ? 'stop' : 'stops'}
          </Typography>
        </Box>
      </CardContent>
    </Card>
  );
}

export default SummaryCard;
