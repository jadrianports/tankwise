// One shared preset-route constant (05-UI-SPEC Preset Routes table). Both the
// PresetRoutes chips and any demo/README reference import from this single
// array so a repeat click always hits the exact same normalized cache key
// (dataset-verified coordinates copied byte-for-byte from the Bruno collection).
export const PRESET_ROUTES = [
  {
    label: 'Denver → Kansas City',
    description: 'Happy path — full itinerary with fuel stops',
    start: '39.7392,-104.9903',
    finish: '39.0997,-94.5786',
  },
  {
    label: 'San Francisco → Seattle',
    description: 'Infeasible gap between fuel stops',
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
