import Alert from '@mui/material/Alert';

// Renders the mapped per-error-code message as escaped JSX text (React
// auto-escapes {message} -- never dangerouslySetInnerHTML, T-05-06). The form
// stays editable underneath; this alert never replaces it.
function ErrorAlert({ error }) {
  return <Alert severity="error">{error.message}</Alert>;
}

export default ErrorAlert;
