// One shared preset-route constant (05-UI-SPEC Preset Routes table). Both the
// PresetRoutes chips and any demo/README reference import from this single
// array so a repeat click always hits the exact same normalized cache key
// (dataset-verified fixed lat,lng coordinates -- never addresses -- copied
// byte-for-byte from the Bruno collection). Ordered happy -> happy -> error
// -> error for the demo walkthrough.
export const PRESET_ROUTES = [
  {
    label: 'Denver → Kansas City',
    description: 'Happy path · single fuel stop',
    start: '39.7392,-104.9903',
    finish: '39.0997,-94.5786',
  },
  {
    label: 'Dallas → Los Angeles',
    description: 'Happy path · multiple fuel stops',
    start: '32.7767,-96.7970',
    finish: '34.0522,-118.2437',
  },
  {
    label: 'San Francisco → Seattle',
    description: 'Infeasible · 500-mi gap between stops',
    start: '37.7749,-122.4194',
    finish: '47.6062,-122.3321',
  },
  {
    label: 'Catalina Island → Los Angeles',
    description: 'No drivable route',
    start: '33.3879,-118.4163',
    finish: '34.0522,-118.2437',
  },
];
