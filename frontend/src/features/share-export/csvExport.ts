// Client-side CSV export (UX-04, D-29): one row per fuel stop, the
// dispatcher-facing artifact the plan text asks for. No backend call, no
// new formatter -- values are written as the backend's own Decimal
// strings, not re-parsed through utils/format.ts (a spreadsheet wants raw
// numbers, not "$1,234.56"-style display strings).
import type { RouteResponse } from '../../types/routeContract';

const CSV_HEADERS = ['Stop #', 'Station Name', 'Station ID', 'Miles From Start', 'Price/Gal', 'Gallons', 'Cost'];

// RFC 4180 minimal escaping: quote a field containing a comma, quote, or
// newline, doubling any embedded quotes.
function csvEscape(value: string): string {
  if (/[",\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

export function buildStopsCsv(data: RouteResponse): string {
  const rows = data.fuel_stops.map((stop, index) => [
    String(index + 1),
    stop.name,
    stop.station_id ?? '',
    stop.distance_from_start_mi,
    stop.price_per_gallon,
    stop.gallons,
    stop.cost,
  ]);
  return [CSV_HEADERS, ...rows].map((row) => row.map(csvEscape).join(',')).join('\r\n');
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export function downloadStopsCsv(data: RouteResponse, filename = 'fuel-stops.csv'): void {
  const blob = new Blob([buildStopsCsv(data)], { type: 'text/csv;charset=utf-8;' });
  triggerDownload(blob, filename);
}
