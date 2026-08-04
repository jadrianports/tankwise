import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import IconButton from '@mui/material/IconButton';
import CloseIcon from '@mui/icons-material/Close';
import Typography from '@mui/material/Typography';

// Authored copy explaining a documented solver behaviour -- unlike
// JustificationPopup, this dialog consumes no route data at all (no
// backend prose field, no response type). It reuses that popup's Dialog
// shell so the two explainers share one accessible pattern.
export interface WhyMultipleStopsPopupProps {
  open: boolean;
  onClose: () => void;
}

function WhyMultipleStopsPopup({ open, onClose }: WhyMultipleStopsPopupProps) {
  return (
    <Dialog open={open} onClose={onClose} aria-labelledby="why-multiple-stops-title" maxWidth="xs" fullWidth>
      <DialogTitle
        id="why-multiple-stops-title"
        sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 2 }}
      >
        <Typography variant="h6" component="span">
          Why multiple fuel stops?
        </Typography>
        <IconButton aria-label="Close" onClick={onClose} size="small">
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent>
        <Typography variant="body1" sx={{ mb: 1 }}>
          Stopping isn't free. Beyond the fuel itself, a stop costs driver time, a detour off the
          highway, and idling -- about $35 on industry operating-cost figures. The planner counts
          that charge, so it only takes a stop when the cheaper fuel there beats the cost of
          pulling over.
        </Typography>
        <Typography variant="body1" sx={{ mb: 1 }}>
          A long haul still has a physical floor. Los Angeles to New York City is about 2,790
          driving miles against a loaded semi's roughly 1,050-mile range, so some stops are
          unavoidable no matter how the fuel is bought. The rest are there because they pay for
          themselves.
        </Typography>
        <Typography variant="body1" sx={{ mb: 1 }}>
          The cost shown is fuel only -- the stop charge shapes the plan but isn't added to your
          total.
        </Typography>
        <Typography variant="body1">
          Tap a numbered stop marker or its row in the itinerary to see the specific reasoning
          behind that stop.
        </Typography>
      </DialogContent>
    </Dialog>
  );
}

export default WhyMultipleStopsPopup;
