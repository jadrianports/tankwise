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
          The planner minimizes total dollars spent on fuel, not the number of stops -- a shorter
          itinerary that costs more is never chosen over a longer one that costs less.
        </Typography>
        <Typography variant="body1" sx={{ mb: 1 }}>
          A long haul has a physical floor set by tank range: Los Angeles to New York City is about
          2,790 driving miles against a loaded semi's roughly 1,050-mile range, so two stops are
          unavoidable no matter how the fuel is bought. Any stop beyond that floor exists because
          fuel was cheap enough there to lower the trip total -- an opportunistic top-up, not a
          detour.
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
