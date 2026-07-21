import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

export interface TankGaugeProps {
  fraction: number; // 0..1
}

// Draining/refilling gauge synced to the leg/stop progression --
// useChaseCam.ts updates `fraction` as fuel is consumed along each leg
// and again when a stop's own purchase is added back; the CSS `width`
// transition below supplies the visible drain/refill motion, no separate
// animation loop needed. Fill color stays fuel amber throughout (never
// destructive red) so the gauge never collides with D-13's skipped-
// station flash color.
function TankGauge({ fraction }: TankGaugeProps) {
  const pct = Math.round(Math.min(1, Math.max(0, fraction)) * 100);

  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
      <Typography variant="body2" sx={{ color: 'common.white', whiteSpace: 'nowrap' }}>
        Fuel
      </Typography>
      <Box
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Fuel tank level"
        sx={{
          position: 'relative',
          flexGrow: 1,
          minWidth: 80,
          height: 12,
          borderRadius: 6,
          bgcolor: 'rgba(255,255,255,0.25)',
          overflow: 'hidden',
        }}
      >
        <Box
          sx={{
            position: 'absolute',
            inset: 0,
            width: `${pct}%`,
            bgcolor: 'fuel.main',
            transition: 'width 900ms ease',
          }}
        />
      </Box>
      <Typography variant="body2" sx={{ color: 'common.white', minWidth: 36, textAlign: 'right' }}>
        {pct}%
      </Typography>
    </Box>
  );
}

export default TankGauge;
