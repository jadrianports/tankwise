"""Tests for the cache-key normalizer (D-11, T-04-03). No DB needed --
pure string-formatting behavior."""
from decimal import Decimal

from django.test import SimpleTestCase

from routing.cache import build_cache_key


def coord(lat, lng):
    return {"kind": "coordinate", "lat": Decimal(str(lat)), "lng": Decimal(str(lng))}


def address(value):
    return {"kind": "address", "value": value}


class CoordinatePrecisionCollapseTests(SimpleTestCase):
    """Two coordinate requests differing only past the 5th decimal place
    produce the same key (D-11)."""

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
    keys (D-11)."""

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
    """Every produced key starts with route:v1: and contains exactly one
    | separator."""

    def test_key_starts_with_prefix_and_has_one_separator(self):
        key = build_cache_key(
            {
                "start": coord("41.8781", "-87.6298"),
                "finish": address("St Louis, MO"),
            }
        )

        self.assertTrue(key.startswith("route:v1:"))
        self.assertEqual(key.count("|"), 1)
