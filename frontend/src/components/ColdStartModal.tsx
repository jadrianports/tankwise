import { useEffect, useState } from 'react';
import Box from '@mui/material/Box';
import CircularProgress from '@mui/material/CircularProgress';
import Dialog from '@mui/material/Dialog';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import IconButton from '@mui/material/IconButton';
import Typography from '@mui/material/Typography';
import CloseIcon from '@mui/icons-material/Close';

import { useColdStart } from '../features/results/useColdStart';
import { useRoutePlanContext } from '../context/RoutePlanContext';

// Dedicated waking overlay (D-08/D-09), distinct from the inline
// LoadingNarration spinner. This is a SECOND CONSUMER of the same
// useColdStart hook LoadingNarration already drives -- the modal owns no
// timer of its own, it only decides when to present the shared 'waking'
// stage as a full dialog instead of an inline sentence. Structurally a
// copy of JustificationPopup's dialog shell (Dialog/DialogTitle/
// DialogContent/IconButton close pattern).
//
// The modal opens on either of two paths into useColdStart's 'waking'
// stage. The FAST path: a genuine "the server did not answer" signal
// (routeClient's own retry loop, or readyClient's pre-warm ping) fired
// past the 4-second escalation threshold. The ELAPSED-ONLY path: past 18
// seconds with no such signal at all, for a request that is hanging
// without ever erroring -- the free-tier hosting proxy holds the
// connection open while a sleeping instance wakes, so the request never
// rejects and never returns a 5xx, it simply takes roughly 50 seconds and
// then returns 200. The 18-second figure is set far enough above the
// slowest measured warm solve that the wake-up claim on that second path
// stays well-founded (see useColdStart.ts for both measured figures).
function ColdStartModal() {
  const { status } = useRoutePlanContext();
  const isLoading = status === 'loading';
  const stage = useColdStart(isLoading);

  // Local dismiss state layered on top of the shared hook: lets the user
  // close the overlay without being trapped behind it for the full retry
  // budget, but resets on every new load so it never stays dismissed for
  // a request the user hasn't seen wake-copy for yet.
  const [dismissed, setDismissed] = useState(false);
  useEffect(() => {
    if (!isLoading) setDismissed(false);
  }, [isLoading]);

  const open = isLoading && stage === 'waking' && !dismissed;

  const handleClose = (_event: unknown, reason: 'backdropClick' | 'escapeKeyDown') => {
    // An accidental backdrop tap must not blow away the explanation of why
    // the app is slow -- only the explicit Close control (below) dismisses
    // it. The Dialog's own escape-key-down suppression prop (set below)
    // means `escapeKeyDown` never reaches here.
    if (reason === 'backdropClick') return;
  };

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      disableEscapeKeyDown
      aria-labelledby="cold-start-modal-title"
      maxWidth="xs"
      fullWidth
    >
      <DialogTitle
        id="cold-start-modal-title"
        sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 2 }}
      >
        <Typography variant="h6" component="span">
          Waking up the server
        </Typography>
        <IconButton aria-label="Close" onClick={() => setDismissed(true)} size="small">
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent>
        <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, py: 2 }}>
          <CircularProgress size={32} />
          <Typography variant="body1" align="center">
            This app runs on a free tier that falls asleep after a few idle minutes, and it&apos;s
            booting back up now -- usually 30 to 60 seconds. Your route will solve as soon as it&apos;s
            awake.
          </Typography>
        </Box>
      </DialogContent>
    </Dialog>
  );
}

export default ColdStartModal;
