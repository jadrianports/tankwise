// Presentation-layer number formatting. The /api/route contract returns
// full-precision Decimal STRINGS for gallons and route miles (only money is
// quantized server-side); these helpers round them for display without
// touching the frozen backend contract.

// "10.36625407619502107691084069" -> "10.37 gal"
export function formatGallons(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value} gal`;
  return `${n.toFixed(2)} gal`;
}

// "603.6625407619502107691084069" -> "604 mi"
export function formatMiles(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value} mi`;
  return `${Math.round(n).toLocaleString('en-US')} mi`;
}
