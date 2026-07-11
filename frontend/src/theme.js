import { createTheme } from '@mui/material/styles';

// Two self-hosted families (imported for real in fonts.js): Space Grotesk for
// headings/display, Inter for body/UI. Kept as constants so every typography
// role references the same string.
const HEADING_FONT = 'Space Grotesk, sans-serif';
const BODY_FONT = 'Inter, sans-serif';

// Custom "Fuel Amber" accent — a dedicated palette key so it is NOT MUI's
// default `secondary` blue. Reserved for fuel-stop markers and per-stop
// price/cost figures only (see 05-UI-SPEC accent-reservation list).
const fuelLight = {
  main: '#F59E0B',
  dark: '#B45309',
  contrastText: '#FFFFFF',
};
const fuelDark = {
  main: '#FBBF24',
  dark: '#D97706',
  contrastText: '#10151B',
};

// Modern MUI theming: the unified ThemeProvider + CSS theme variables API
// (createTheme with cssVariables + colorSchemes), not the superseded
// experimental provider. Light is the default scheme; the class selector lets
// InitColorSchemeScript + useColorScheme toggle dark mode via a class on <html>.
const theme = createTheme({
  cssVariables: {
    colorSchemeSelector: 'class',
  },
  colorSchemes: {
    light: {
      palette: {
        primary: { main: '#0F6D4F', dark: '#0A4F39', light: '#3E9678' },
        fuel: fuelLight,
        error: { main: '#D32F2F' },
        background: { default: '#F7F8FA', paper: '#FFFFFF' },
        text: { primary: '#1A2027', secondary: '#5B6472' },
        divider: '#E1E4E8',
      },
    },
    dark: {
      palette: {
        primary: { main: '#34C796', dark: '#1F8F68', light: '#6FE0B8' },
        fuel: fuelDark,
        error: { main: '#EF5350' },
        background: { default: '#10151B', paper: '#1B222B' },
        text: { primary: '#EDEFF2', secondary: '#A0AAB6' },
        divider: '#2B333D',
      },
    },
  },
  // Exactly 4 sizes / 2 weights (05-UI-SPEC Typography). No 700 weight — the
  // spec forbids a third weight; use color/size contrast for emphasis instead.
  typography: {
    fontFamily: BODY_FONT,
    // Body — 16 / 400 / 1.5, Inter
    body1: { fontFamily: BODY_FONT, fontSize: '1rem', fontWeight: 400, lineHeight: 1.5 },
    // Label — 14 / 400 / 1.4, Inter
    body2: { fontFamily: BODY_FONT, fontSize: '0.875rem', fontWeight: 400, lineHeight: 1.4 },
    // Heading — 20 / 600 / 1.25, Space Grotesk
    h6: { fontFamily: HEADING_FONT, fontSize: '1.25rem', fontWeight: 600, lineHeight: 1.25 },
    subtitle1: { fontFamily: HEADING_FONT, fontSize: '1.25rem', fontWeight: 600, lineHeight: 1.25 },
    // Display — 28 / 600 / 1.2, Space Grotesk
    h5: { fontFamily: HEADING_FONT, fontSize: '1.75rem', fontWeight: 600, lineHeight: 1.2 },
    h4: { fontFamily: HEADING_FONT, fontSize: '1.75rem', fontWeight: 600, lineHeight: 1.2 },
  },
});

export default theme;
