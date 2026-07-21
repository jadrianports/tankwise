import { test } from 'node:test';
import assert from 'node:assert/strict';

import { buildStopsCsv } from './csvExport.ts';

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
};

test('buildStopsCsv writes the D-29 header row', () => {
  const csv = buildStopsCsv(FIXTURE);
  const [header] = csv.split('\r\n');
  assert.equal(header, 'Stop #,Station Name,Station ID,Miles From Start,Price/Gal,Gallons,Cost');
});

test('buildStopsCsv writes one row per fuel stop, numbered from 1', () => {
  const csv = buildStopsCsv(FIXTURE);
  const rows = csv.split('\r\n');
  assert.equal(rows.length, 3); // header + 2 stops
  assert.equal(rows[1], '1,Pilot Travel Center,ST-1,210.5,3.459,58.62,202.79');
});

test('buildStopsCsv falls back to an empty station id field, never the string "null"', () => {
  const csv = buildStopsCsv(FIXTURE);
  const rows = csv.split('\r\n');
  assert.equal(rows[2], '2,Loves Travel Stop,,640.2,3.512,40.10,140.86');
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
  });
  const rows = csv.split('\r\n');
  assert.equal(rows[1], '1,"Loves, Exit 42",ST-2,100,3.5,10,35');
});
