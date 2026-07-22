import { expect, test, vi } from 'vitest';

import { buildStopsCsv, downloadStopsCsv } from './csvExport';
import type { RouteResponse } from '../../types/routeContract';

// Only `fuel_stops` is exercised by buildStopsCsv -- the fixture is cast
// through `unknown` rather than filling in every unrelated RouteResponse
// field this function never reads.
const FIXTURE = {
  fuel_stops: [
    {
      name: 'Pilot Travel Center',
      station_id: 'ST-1',
      distance_from_start_mi: '210.5',
      price_per_gallon: '3.459',
      gallons: '58.62',
      cost: '202.79',
    },
    {
      name: 'Loves Travel Stop',
      station_id: null,
      distance_from_start_mi: '640.2',
      price_per_gallon: '3.512',
      gallons: '40.10',
      cost: '140.86',
    },
  ],
} as unknown as RouteResponse;

test('buildStopsCsv writes the expected header row', () => {
  const csv = buildStopsCsv(FIXTURE);
  const [header] = csv.split('\r\n');
  expect(header).toBe('Stop #,Station Name,Station ID,Miles From Start,Price/Gal,Gallons,Cost');
});

test('buildStopsCsv writes one row per fuel stop, numbered from 1', () => {
  const csv = buildStopsCsv(FIXTURE);
  const rows = csv.split('\r\n');
  expect(rows.length).toBe(3); // header + 2 stops
  expect(rows[1]).toBe('1,Pilot Travel Center,ST-1,210.5,3.459,58.62,202.79');
});

test('buildStopsCsv falls back to an empty station id field, never the string "null"', () => {
  const csv = buildStopsCsv(FIXTURE);
  const rows = csv.split('\r\n');
  expect(rows[2]).toBe('2,Loves Travel Stop,,640.2,3.512,40.10,140.86');
});

test('buildStopsCsv quotes a station name containing a comma', () => {
  const csv = buildStopsCsv({
    fuel_stops: [
      {
        name: 'Loves, Exit 42',
        station_id: 'ST-2',
        distance_from_start_mi: '100',
        price_per_gallon: '3.5',
        gallons: '10',
        cost: '35',
      },
    ],
  } as unknown as RouteResponse);
  const rows = csv.split('\r\n');
  expect(rows[1]).toBe('1,"Loves, Exit 42",ST-2,100,3.5,10,35');
});

test('downloadStopsCsv triggers exactly one anchor click without throwing', () => {
  const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  expect(() => downloadStopsCsv(FIXTURE)).not.toThrow();
  expect(clickSpy).toHaveBeenCalledOnce();
  clickSpy.mockRestore();
});

test('downloadStopsCsv sets the default filename on the triggered anchor', () => {
  let capturedFilename = '';
  const clickSpy = vi
    .spyOn(HTMLAnchorElement.prototype, 'click')
    .mockImplementation(function (this: HTMLAnchorElement) {
      capturedFilename = this.download;
    });
  downloadStopsCsv(FIXTURE);
  expect(capturedFilename).toBe('fuel-stops.csv');
  clickSpy.mockRestore();
});

test('downloadStopsCsv honours a caller-supplied filename', () => {
  let capturedFilename = '';
  const clickSpy = vi
    .spyOn(HTMLAnchorElement.prototype, 'click')
    .mockImplementation(function (this: HTMLAnchorElement) {
      capturedFilename = this.download;
    });
  downloadStopsCsv(FIXTURE, 'custom-stops.csv');
  expect(capturedFilename).toBe('custom-stops.csv');
  clickSpy.mockRestore();
});
