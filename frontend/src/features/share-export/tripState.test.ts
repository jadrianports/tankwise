import { expect, test, vi } from 'vitest';

import {
  decodeTripState,
  encodeTripState,
  getLoadTripRequestSnapshot,
  requestLoadTrip,
  subscribeLoadTripRequest,
  tripStateToQueryString,
  type TripState,
} from './tripState';

const TRIP: TripState = {
  start: '34.0522,-118.2437',
  finish: '40.7128,-74.0060',
  startLabel: 'Los Angeles',
  finishLabel: 'New York City',
  vehicle: 'semi-loaded',
};

const MULTI_STOP_TRIP: TripState = {
  start: '34.0522,-118.2437',
  finish: '41.8781,-87.6298',
  startLabel: 'Los Angeles',
  finishLabel: 'Chicago',
  vehicle: 'semi-loaded',
  waypoints: ['39.7392,-104.9903'],
  stopCount: 3,
};

test('encodeTripState carries every field under its real parameter key', () => {
  const params = encodeTripState(TRIP);
  expect(params.get('start')).toBe(TRIP.start);
  expect(params.get('finish')).toBe(TRIP.finish);
  expect(params.get('from')).toBe(TRIP.startLabel);
  expect(params.get('to')).toBe(TRIP.finishLabel);
  expect(params.get('vehicle')).toBe(TRIP.vehicle);
});

test('tripStateToQueryString round-trips through decodeTripState back to an equal TripState', () => {
  const queryString = tripStateToQueryString(TRIP);
  expect(decodeTripState(queryString)).toEqual(TRIP);
});

test('decodeTripState returns null when the start parameter is missing', () => {
  const params = encodeTripState(TRIP);
  params.delete('start');
  expect(decodeTripState(params)).toBeNull();
});

test('decodeTripState returns null when the finish parameter is missing', () => {
  const params = encodeTripState(TRIP);
  params.delete('finish');
  expect(decodeTripState(params)).toBeNull();
});

test('decodeTripState falls back to the raw coordinate string for a label when the label parameter is absent', () => {
  const params = new URLSearchParams();
  params.set('start', TRIP.start);
  params.set('finish', TRIP.finish);
  const trip = decodeTripState(params);
  expect(trip?.startLabel).toBe(TRIP.start);
  expect(trip?.finishLabel).toBe(TRIP.finish);
});

test('decodeTripState falls back to the hero vehicle preset id when the vehicle parameter is absent', () => {
  const params = new URLSearchParams();
  params.set('start', TRIP.start);
  params.set('finish', TRIP.finish);
  const trip = decodeTripState(params);
  expect(trip?.vehicle).toBe('semi-loaded');
});

test('decodeTripState accepts a pre-built URLSearchParams as well as a raw query string', () => {
  const fromParams = decodeTripState(encodeTripState(TRIP));
  const fromString = decodeTripState(tripStateToQueryString(TRIP));
  expect(fromParams).toEqual(fromString);
  expect(fromParams).toEqual(TRIP);
});

test('encodeTripState carries an ordered waypoints list and a derived stopCount hint', () => {
  const params = encodeTripState(MULTI_STOP_TRIP);
  expect(params.get('stops')).toBe('39.7392,-104.9903');
  expect(params.get('stopCount')).toBe('3');
});

test('encodeTripState omits stops/stopCount entirely for a plain A-to-B trip', () => {
  const params = encodeTripState(TRIP);
  expect(params.has('stops')).toBe(false);
  expect(params.has('stopCount')).toBe(false);
});

test('a multi-stop trip round-trips its ordered waypoints and stopCount through decodeTripState', () => {
  const queryString = tripStateToQueryString(MULTI_STOP_TRIP);
  expect(decodeTripState(queryString)).toEqual(MULTI_STOP_TRIP);
});

test('an old A-to-B share link with no stops param decodes to the identical TripState as before D-10', () => {
  const params = encodeTripState(TRIP);
  expect(params.has('stops')).toBe(false);
  const trip = decodeTripState(params);
  expect(trip).toEqual(TRIP);
  expect(trip?.waypoints).toBeUndefined();
  expect(trip?.stopCount).toBeUndefined();
  expect(trip?.staleClientWarning).toBeUndefined();
});

test('a link whose stopCount hint exceeds the recovered waypoints yields a stale-client warning, never a silent 2-point collapse', () => {
  // Simulates a stale bundle that could not fully parse `stops` --
  // encoded by an originating client expecting 3 total stops, but this
  // decode only recovers the 2 endpoints (no `stops` param at all).
  const params = new URLSearchParams();
  params.set('start', MULTI_STOP_TRIP.start);
  params.set('finish', MULTI_STOP_TRIP.finish);
  params.set('stopCount', '3');

  const trip = decodeTripState(params);
  expect(trip).not.toBeNull();
  expect(trip?.start).toBe(MULTI_STOP_TRIP.start);
  expect(trip?.finish).toBe(MULTI_STOP_TRIP.finish);
  expect(trip?.waypoints).toBeUndefined();
  expect(trip?.staleClientWarning).toBe(
    'This shared trip expected 3 stops but only 2 loaded — refresh for the full route.'
  );
});

test('a stopCount hint that matches the recovered waypoints produces no stale-client warning', () => {
  const trip = decodeTripState(tripStateToQueryString(MULTI_STOP_TRIP));
  expect(trip?.staleClientWarning).toBeUndefined();
});

test('requestLoadTrip makes getLoadTripRequestSnapshot return the supplied trip and notifies subscribers', () => {
  const listener = vi.fn();
  const unsubscribe = subscribeLoadTripRequest(listener);
  requestLoadTrip(TRIP);
  expect(listener).toHaveBeenCalledOnce();
  expect(getLoadTripRequestSnapshot()?.trip).toEqual(TRIP);
  unsubscribe();
});

test('requestLoadTrip increments the nonce on a repeat request with the same trip', () => {
  const firstNonce = getLoadTripRequestSnapshot()?.nonce ?? 0;
  requestLoadTrip(TRIP);
  const secondNonce = getLoadTripRequestSnapshot()?.nonce ?? 0;
  expect(secondNonce).toBeGreaterThan(firstNonce);
});

test('subscribeLoadTripRequest stops invoking its listener after the returned unsubscribe function is called', () => {
  const listener = vi.fn();
  const unsubscribe = subscribeLoadTripRequest(listener);
  requestLoadTrip(TRIP);
  expect(listener).toHaveBeenCalledOnce();
  unsubscribe();
  requestLoadTrip(TRIP);
  expect(listener).toHaveBeenCalledOnce();
});
