import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';

import ShareExportBar from './ShareExportBar';
import { downloadStopsCsv } from './csvExport';
import { downloadTripGeoJson } from './geoJsonExport';
import type { RouteResponse } from '../../types/routeContract';

// Mocked at the module boundary -- the component's contract is that it
// calls the download function with the current data, not that a Blob
// reaches the DOM, and the download path itself already has its own
// direct coverage.
vi.mock('./csvExport', () => ({
  downloadStopsCsv: vi.fn(),
}));
vi.mock('./geoJsonExport', () => ({
  downloadTripGeoJson: vi.fn(),
}));

const mockedDownloadStopsCsv = vi.mocked(downloadStopsCsv);
const mockedDownloadTripGeoJson = vi.mocked(downloadTripGeoJson);

// This file's vite config runs without vitest's `globals` option, so
// testing-library's auto-cleanup detection never fires -- each render
// must be torn down explicitly between tests.
afterEach(cleanup);

beforeEach(() => {
  mockedDownloadStopsCsv.mockReset();
  mockedDownloadTripGeoJson.mockReset();
});

// The component passes this straight through to the mocked download
// functions and reads none of its fields itself.
const FIXTURE_DATA = { total_cost: '245.67' } as unknown as RouteResponse;
const SHARE_URL = 'https://example.test/share/abc123';

test('all three buttons render disabled when data is null', () => {
  render(<ShareExportBar data={null} shareUrl={SHARE_URL} />);

  expect(screen.getByRole('button', { name: 'Copy share link' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Export CSV' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Export GeoJSON' })).toBeDisabled();
});

test('all three buttons render disabled when the share URL is null', () => {
  render(<ShareExportBar data={FIXTURE_DATA} shareUrl={null} />);

  expect(screen.getByRole('button', { name: 'Copy share link' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Export CSV' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Export GeoJSON' })).toBeDisabled();
});

test('clicking Export CSV calls the CSV download function once with the supplied data', () => {
  render(<ShareExportBar data={FIXTURE_DATA} shareUrl={SHARE_URL} />);

  fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));

  expect(mockedDownloadStopsCsv).toHaveBeenCalledTimes(1);
  expect(mockedDownloadStopsCsv).toHaveBeenCalledWith(FIXTURE_DATA);
});

test('clicking Export GeoJSON calls the GeoJSON download function once with the supplied data', () => {
  render(<ShareExportBar data={FIXTURE_DATA} shareUrl={SHARE_URL} />);

  fireEvent.click(screen.getByRole('button', { name: 'Export GeoJSON' }));

  expect(mockedDownloadTripGeoJson).toHaveBeenCalledTimes(1);
  expect(mockedDownloadTripGeoJson).toHaveBeenCalledWith(FIXTURE_DATA);
});

test('a resolving clipboard write flips the copy button label to the copied state', async () => {
  const originalClipboard = navigator.clipboard;
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    configurable: true,
  });

  try {
    render(<ShareExportBar data={FIXTURE_DATA} shareUrl={SHARE_URL} />);

    fireEvent.click(screen.getByRole('button', { name: 'Copy share link' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Link copied' })).toBeInTheDocument();
    });
  } finally {
    Object.defineProperty(navigator, 'clipboard', {
      value: originalClipboard,
      configurable: true,
    });
  }
});

test('a rejecting clipboard write renders the manual-copy field carrying the share URL', async () => {
  const originalClipboard = navigator.clipboard;
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    configurable: true,
  });

  try {
    render(<ShareExportBar data={FIXTURE_DATA} shareUrl={SHARE_URL} />);

    fireEvent.click(screen.getByRole('button', { name: 'Copy share link' }));

    await waitFor(() => {
      expect(screen.getByLabelText('Copy this link manually')).toHaveValue(SHARE_URL);
    });
  } finally {
    Object.defineProperty(navigator, 'clipboard', {
      value: originalClipboard,
      configurable: true,
    });
  }
});
