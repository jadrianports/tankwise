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
