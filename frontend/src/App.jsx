import { useState, useEffect } from 'react'
import { useColorScheme } from '@mui/material/styles'
import AppBar from '@mui/material/AppBar'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'
import IconButton from '@mui/material/IconButton'
import Box from '@mui/material/Box'
import Brightness4 from '@mui/icons-material/Brightness4'
import Brightness7 from '@mui/icons-material/Brightness7'

// Dark-mode toggle wired to MUI's useColorScheme (not a hand-rolled
// useState/localStorage). Guard the first render where `mode` is undefined to
// avoid a hydration/SSR mismatch, per the current MUI dark-mode guidance.
function ColorModeToggle() {
  const { mode, setMode } = useColorScheme()
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  if (!mounted) {
    return <IconButton color="inherit" aria-label="toggle color mode" />
  }

  const isDark = mode === 'dark'
  return (
    <IconButton
      color="inherit"
      aria-label={isDark ? 'switch to light mode' : 'switch to dark mode'}
      onClick={() => setMode(isDark ? 'light' : 'dark')}
    >
      {isDark ? <Brightness7 /> : <Brightness4 />}
    </IconButton>
  )
}

// Placeholder shell — exercises the custom palette, the Space Grotesk / Inter
// pairing, and a working dark toggle so the themed foundation is verifiable.
// 05-03 replaces this with the full sidebar + map layout.
function App() {
  return (
    <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
      <AppBar position="static" color="primary">
        <Toolbar sx={{ px: { xs: 2, sm: 4 } }}>
          <Typography variant="h5" component="h1" sx={{ flexGrow: 1 }}>
            Fuel Route Optimizer
          </Typography>
          <ColorModeToggle />
        </Toolbar>
      </AppBar>

      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 1,
          height: 'calc(100vh - 64px)',
          px: 3,
          textAlign: 'center',
        }}
      >
        <Typography variant="h6" component="h2">
          Plan a route
        </Typography>
        <Typography variant="body1" color="text.secondary">
          Enter a start and finish location to see the cheapest fueling plan.
        </Typography>
      </Box>
    </Box>
  )
}

export default App
