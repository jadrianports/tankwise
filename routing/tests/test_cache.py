"""Tests for the cache-key normalizer. No DB needed --
pure string-formatting behavior."""
from decimal import Decimal

from django.test import SimpleTestCase

from routing.cache import build_cache_key


def coord(lat, lng):
    return {"kind": "coordinate", "lat": Decimal(str(lat)), "lng": Decimal(str(lng))}


def address(value):
    return {"kind": "address", "value": value}


def vehicle(mpg=None, tank_range_mi=None, starting_fuel=None):
    """Build a validated-data-shaped vehicle dict, Decimal-valued like
    RouteRequestSerializer.validated_data["vehicle"], with any
    unspecified key simply omitted (letting _vehicle_token supply its
    own default for that key)."""
    result = {}
    if mpg is not None:
        result["mpg"] = Decimal(str(mpg))
    if tank_range_mi is not None:
        result["tank_range_mi"] = Decimal(str(tank_range_mi))
    if starting_fuel is not None:
        result["starting_fuel"] = Decimal(str(starting_fuel))
    return result


class CoordinatePrecisionCollapseTests(SimpleTestCase):
    """Two coordinate requests differing only past the 5th decimal place
    produce the same key."""

    def test_sixth_decimal_difference_collapses_to_same_key(self):
        key1 = build_cache_key(
            {
                "start": coord("41.878100", "-87.629800"),
                "finish": coord("38.627000", "-90.199400"),
            }
        )
        key2 = build_cache_key(
            {
                "start": coord("41.8781004", "-87.6298001"),
                "finish": coord("38.6270003", "-90.1994002"),
            }
        )

        self.assertEqual(key1, key2)


class AddressNormalizationTests(SimpleTestCase):
    """Two addresses differing only in case/whitespace produce identical
    keys."""

    def test_case_and_whitespace_variants_collapse_to_same_key(self):
        key1 = build_cache_key(
            {"start": address("123 Main St"), "finish": address("456 Oak Ave")}
        )
        key2 = build_cache_key(
            {"start": address("  123   MAIN st "), "finish": address("456 OAK ave")}
        )

        self.assertEqual(key1, key2)


class NamespaceCollisionTests(SimpleTestCase):
    """A coordinate token and an address token with the same underlying
    characters must never collide (explicit c:/a: prefixes)."""

    def test_coordinate_and_lookalike_address_string_produce_distinct_keys(self):
        coord_key = build_cache_key(
            {
                "start": coord("41.87810", "-87.62980"),
                "finish": coord("38.62700", "-90.19940"),
            }
        )
        # An address whose text happens to equal the coordinate token's
        # post-normalization body would collide without the prefix.
        addr_key = build_cache_key(
            {
                "start": address("41.8781,-87.6298"),
                "finish": address("38.627,-90.1994"),
            }
        )

        self.assertNotEqual(coord_key, addr_key)


class MixedRequestStabilityTests(SimpleTestCase):
    """A mixed coord+address request produces a stable, distinct key."""

    def test_mixed_request_is_stable_and_distinct(self):
        key1 = build_cache_key(
            {"start": coord("41.8781", "-87.6298"), "finish": address("St Louis, MO")}
        )
        key2 = build_cache_key(
            {"start": coord("41.8781", "-87.6298"), "finish": address("St Louis, MO")}
        )
        coord_only_key = build_cache_key(
            {
                "start": coord("41.8781", "-87.6298"),
                "finish": coord("38.6270", "-90.1994"),
            }
        )

        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, coord_only_key)


class KeyFormatTests(SimpleTestCase):
    """Every produced key starts with route:v2: and contains exactly two
    | separators (start|finish|vehicle)."""

    def test_key_starts_with_prefix_and_has_two_separators(self):
        key = build_cache_key(
            {
                "start": coord("41.8781", "-87.6298"),
                "finish": address("St Louis, MO"),
            }
        )

        self.assertTrue(key.startswith("route:v2:"))
        self.assertEqual(key.count("|"), 2)


class VehicleCacheKeyTests(SimpleTestCase):
    """Cache keys are vehicle-aware: two profiles never collide, a
    near-identical profile still hits, and a missing vehicle key
    resolves identically to the explicit defaults."""

    def _payload(self, vehicle=None):
        payload = {
            "start": coord("41.8781", "-87.6298"),
            "finish": coord("38.6270", "-90.1994"),
        }
        if vehicle is not None:
            payload["vehicle"] = vehicle
        return payload

    def test_different_mpg_produces_different_key(self):
        key_a = build_cache_key(self._payload(vehicle(mpg="6")))
        key_b = build_cache_key(self._payload(vehicle(mpg="10")))

        self.assertNotEqual(key_a, key_b)

    def test_mpg_precision_collapses_to_same_key(self):
        key_a = build_cache_key(self._payload(vehicle(mpg="6")))
        key_b = build_cache_key(self._payload(vehicle(mpg="6.00")))

        self.assertEqual(key_a, key_b)

    def test_absent_vehicle_matches_explicit_defaults(self):
        key_absent = build_cache_key(self._payload())
        key_explicit = build_cache_key(
            self._payload(vehicle(mpg="10", tank_range_mi="500", starting_fuel="1.0"))
        )

        self.assertEqual(key_absent, key_explicit)

    def test_every_key_starts_with_v2_prefix(self):
        for payload in (
            self._payload(),
            self._payload(vehicle(mpg="6")),
            self._payload(vehicle(tank_range_mi="1800")),
        ):
            self.assertTrue(build_cache_key(payload).startswith("route:v2:"))

    def test_no_generated_key_contains_v1_substring(self):
        profiles = [
            None,
            vehicle(mpg="6"),
            vehicle(mpg="32", tank_range_mi="400"),
            vehicle(starting_fuel="0.25"),
            vehicle(mpg="100", tank_range_mi="2000", starting_fuel="0.0"),
        ]
        for profile in profiles:
            self.assertNotIn("route:v1:", build_cache_key(self._payload(profile)))

    def test_spread_of_distinct_profiles_yields_no_key_collisions(self):
        profiles = [
            vehicle(mpg="6", tank_range_mi="1800", starting_fuel="1.0"),
            vehicle(mpg="10", tank_range_mi="500", starting_fuel="1.0"),
            vehicle(mpg="32", tank_range_mi="400", starting_fuel="1.0"),
            vehicle(mpg="10", tank_range_mi="500", starting_fuel="0.5"),
            vehicle(mpg="10", tank_range_mi="500", starting_fuel="0.0"),
            vehicle(mpg="1", tank_range_mi="20", starting_fuel="0.0"),
            vehicle(mpg="100", tank_range_mi="2000", starting_fuel="1.0"),
        ]
        keys = [build_cache_key(self._payload(profile)) for profile in profiles]

        self.assertEqual(len(keys), len(set(keys)))
