// Presentation-layer number formatting. The /api/route contract returns
// full-precision Decimal STRINGS for gallons and route miles (only money is
// quantized server-side); these helpers round them for display without
// touching the frozen backend contract.

type Numberish = string | number | null | undefined;

// "10.36625407619502107691084069" -> "10.37 gal"
export function formatGallons(value: Numberish): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value} gal`;
  return `${n.toFixed(2)} gal`;
}

// "603.6625407619502107691084069" -> "604 mi"
export function formatMiles(value: Numberish): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value} mi`;
  return `${Math.round(n).toLocaleString('en-US')} mi`;
}

// Seconds (total_duration_s / legs[].duration_s, already plain numbers
// server-side -- see _duration_repr) -> "15h 20m"; under an hour -> "Xm".
export function formatDuration(value: Numberish): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value}`;
  const totalMinutes = Math.round(n / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours <= 0) return `${minutes}m`;
  return `${hours}h ${minutes}m`;
}

// Already-numeric percent (price_percentile, savings.percent -- see
// _percent_repr, which returns a float, not a Decimal string) -> "12.5%".
export function formatPercent(value: Numberish): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value}%`;
  return `${n.toFixed(1)}%`;
}

// Money is already quantized to 2dp server-side; this only adds the `$`
// prefix and a thousands separator for display.
export function formatCurrency(value: Numberish): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return `$${value}`;
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
