import { useEffect, useState } from 'react';
import { useColorScheme } from '@mui/material/styles';
import AppBar from '@mui/material/AppBar';
import Toolbar from '@mui/material/Toolbar';
import Typography from '@mui/material/Typography';
import IconButton from '@mui/material/IconButton';
import Brightness4 from '@mui/icons-material/Brightness4';
import Brightness7 from '@mui/icons-material/Brightness7';

// Dark-mode toggle wired to MUI's useColorScheme (not a hand-rolled
// useState/localStorage). Guard the first render where `mode` is undefined to
// avoid a hydration/first-paint mismatch.
function ColorModeToggle() {
  const { mode, setMode } = useColorScheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) {
    return <IconButton color="inherit" aria-label="toggle color mode" />;
  }

  const isDark = mode === 'dark';
  return (
    <IconButton
      color="inherit"
      aria-label={isDark ? 'switch to light mode' : 'switch to dark mode'}
      onClick={() => setMode(isDark ? 'light' : 'dark')}
    >
      {isDark ? <Brightness7 /> : <Brightness4 />}
    </IconButton>
  );
}

// App-wide top bar: title (Display role) + dark-mode toggle. MUI default
// AppBar height (64px desktop / 56px mobile) -- no override needed.
function AppShell() {
  return (
    <AppBar position="static" color="primary">
      <Toolbar sx={{ px: { xs: 2, sm: 4 } }}>
        <Typography variant="h5" component="h1" sx={{ flexGrow: 1 }}>
          Fuel Route Optimizer
        </Typography>
        <ColorModeToggle />
      </Toolbar>
    </AppBar>
  );
}

export default AppShell;
