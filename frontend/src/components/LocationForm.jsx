import Box from '@mui/material/Box';
import TextField from '@mui/material/TextField';
import Button from '@mui/material/Button';
import LinearProgress from '@mui/material/LinearProgress';

// Two free-text fields accepting either an address or a `lat,lng` string
// (the polymorphic API, D-06). Controlled by the parent (App) so a preset
// Chip click can fill both fields and this same form submits them. Fields +
// submit disable while a request is in flight; the Button's built-in
// `loading` prop handles the spinner.
function LocationForm({ start, finish, onStartChange, onFinishChange, status, onSubmit }) {
  const isLoading = status === 'loading';

  const handleSubmit = (event) => {
    event.preventDefault();
    if (!start.trim() || !finish.trim() || isLoading) return;
    onSubmit(start.trim(), finish.trim());
  };

  return (
    <Box component="form" onSubmit={handleSubmit} sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <TextField
        label="Start"
        placeholder="e.g. 39.7392,-104.9903"
        helperText="Address or lat,lng"
        value={start}
        onChange={(event) => onStartChange(event.target.value)}
        disabled={isLoading}
        fullWidth
        size="small"
      />
      <TextField
        label="Finish"
        placeholder="e.g. 39.0997,-94.5786"
        helperText="Address or lat,lng"
        value={finish}
        onChange={(event) => onFinishChange(event.target.value)}
        disabled={isLoading}
        fullWidth
        size="small"
      />
      <Box>
        <Button type="submit" variant="contained" color="primary" loading={isLoading} fullWidth>
          Find Cheapest Route
        </Button>
        {isLoading && <LinearProgress sx={{ mt: 1 }} />}
      </Box>
    </Box>
  );
}

export default LocationForm;
